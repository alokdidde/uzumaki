"""Deterministic offline fixture environment and independent evaluator for ``pick-place-v1``.

This is the issue-08 stand-in for the simulator adapter and the environment evaluator. It is not a
physics simulator: kinematics are scripted (a successful ``place`` puts the box exactly at the
commanded pose, it stops moving ``SETTLE_DELAY_MS`` after release, and there is no contact model,
so ``collision`` cannot occur here). Every value is a fixture constant, not a measurement.

``FixtureWorld`` holds privileged state (true box pose, gripper, goal and admission log) and
implements the four declared tools as host handlers behind ``uzumaki.broker``. It decides admission
itself (fresh latest observation, goal active, reachable pose, held/not held), so a request that
the harness should not have sent is still rejected without effect. ``FixtureEvaluator`` reads only
that privileged state and the harness's run status; skill reports are recorded as claims and never
establish progress or success. ``FixtureBackend`` routes the harness's tool calls to the broker and
its ``monitor`` calls to the evaluator, and injects worker crashes and goal withdrawal.

Fixture cases are a frozen ``uzumaki.baseline-cases/1.0`` file of geometry, faults and expected
outcomes. Errors raise ``CaseError`` with a fixed reason and never echo content.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from uzumaki.broker import PICK_PLACE_V1_TOOL_VERSION, Handler, pick_place_v1_broker
from uzumaki.evidence import TOOL_RESULT_STATUSES, ToolResult
from uzumaki.faults import WorkerFailure
from uzumaki.lifecycle import STATUS_STATES, TERMINAL_STATES
from uzumaki.registry import PICK_PLACE_V1_OUTCOMES, PROGRESS

CASES_FORMAT = "uzumaki.baseline-cases/1.0"
TASK_PROTOCOL = "pick-place-v1"
#: Pseudo-tool name of the independent case monitor (evaluator plus goal channel).
MONITOR_TOOL = "monitor"
OBSERVATION_SOURCE = "fixture_world"

#: Protocol values (``docs/protocol/task-v1.json``).
MAX_OBSERVATION_AGE_MS = 200
TABLE_BOUND_M = 0.25
XY_TOLERANCE_M = 0.02
YAW_TOLERANCE_DEG = 10.0
SETTLE_WINDOW_MS = 500
WALL_MS = 120_000

#: Fixture constants (not measurements).
CASE_START_MS = 1000
TOOL_MS: Mapping[str, int] = MappingProxyType({"observe": 100, "pick": 1500, "place": 1500, "abort": 100})
SETTLE_DELAY_MS = 200
STALE_LAG_MS = 300
GRASP_TOLERANCE_M = 0.01
#: Pose estimate error added to the true pose in every observation (x m, y m, yaw deg).
ESTIMATE_OFFSET = (0.002, -0.001, 0.5)
MAX_RESULT_BYTES = 4096
MAX_CASES_BYTES = 1 << 20
MAX_CASES = 64

ACTUATING_TOOLS = frozenset({"pick", "place"})
#: Attempt diagnostics the evaluator accepts from the harness as the last failure.
DIAGNOSTIC_OUTCOMES = frozenset(
    {
        "ambiguous_goal",
        "misgrasp",
        "missing_observation",
        "placement_failure",
        "tool_rejected",
        "unreachable_target",
    }
)

_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,47}")
_CASE_KEYS = frozenset(
    {
        "case_id",
        "geometry",
        "box_size_m",
        "initial_xy_m",
        "initial_yaw_deg",
        "target_xy_m",
        "faults",
        "expected",
    }
)
_FAULT_KEYS = frozenset(
    {"misgrasp_picks", "stale_observes", "crash_on_call", "withdraw_goal_after_call"}
)
_EXPECTED_KEYS = frozenset({"primary", "progress", "state", "status", "attempts", "admitted"})


class CaseError(ValueError):
    """A fixture case file or case was rejected. ``reason`` is a fixed code."""

    REASONS = frozenset({"malformed", "oversized", "unsupported_format", "duplicate_case"})

    def __init__(self, reason: str, subject: str | None = None) -> None:
        if reason not in self.REASONS:
            raise AssertionError("unknown case reason")
        self.reason = reason
        self.subject = subject
        super().__init__(reason if subject is None else f"{reason}: {subject}")


class FixtureClock:
    """Integer-millisecond fake clock shared by the world, the harness and the recorder."""

    def __init__(self, start: int = CASE_START_MS) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@dataclass(frozen=True)
class FixtureCase:
    """One frozen fixture case: geometry, injected faults and the expected outcome."""

    case_id: str
    geometry: str
    box_size_m: float
    initial_xy_m: tuple[float, float]
    initial_yaw_deg: float
    target_xy_m: tuple[float, float]
    misgrasp_picks: int = 0
    stale_observes: tuple[int, ...] = ()
    crash_on_call: int | None = None
    withdraw_goal_after_call: int | None = None
    expected: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


def _closed(value: Any, keys: frozenset[str], subject: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CaseError("malformed", subject)
    return value


def _number(value: Any, low: float, high: float, subject: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise CaseError("malformed", subject)
    return float(value)


def _pair(value: Any, bound: float, subject: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise CaseError("malformed", subject)
    return (_number(value[0], -bound, bound, subject), _number(value[1], -bound, bound, subject))


def _count(value: Any, low: int, subject: str) -> int:
    if type(value) is not int or not low <= value <= 1000:
        raise CaseError("malformed", subject)
    return int(value)


def _optional_count(value: Any, subject: str) -> int | None:
    return None if value is None else _count(value, 1, subject)


def _expected(value: Any, subject: str) -> Mapping[str, Any]:
    data = _closed(value, _EXPECTED_KEYS, subject)
    if data["primary"] not in PICK_PLACE_V1_OUTCOMES or data["progress"] not in PROGRESS:
        raise CaseError("malformed", subject)
    if data["state"] not in TERMINAL_STATES or data["status"] not in STATUS_STATES:
        raise CaseError("malformed", subject)
    if type(data["attempts"]) is not int or not 1 <= data["attempts"] <= 3:
        raise CaseError("malformed", subject)
    admitted = _closed(data["admitted"], frozenset({"pick", "place"}), subject)
    for amount in admitted.values():
        _count(amount, 0, subject)
    return MappingProxyType(copy.deepcopy(data))


def parse_case(entry: Any) -> FixtureCase:
    """Validate one case object of a ``uzumaki.baseline-cases/1.0`` file."""
    data = _closed(entry, _CASE_KEYS, "case")
    case_id = data["case_id"]
    if not isinstance(case_id, str) or not _ID.fullmatch(case_id):
        raise CaseError("malformed", "case_id")
    if not isinstance(data["geometry"], str) or not _ID.fullmatch(data["geometry"]):
        raise CaseError("malformed", case_id)
    faults = _closed(data["faults"], _FAULT_KEYS, case_id)
    stale = faults["stale_observes"]
    if not isinstance(stale, list) or len(stale) > 16:
        raise CaseError("malformed", case_id)
    return FixtureCase(
        case_id=case_id,
        geometry=data["geometry"],
        box_size_m=_number(data["box_size_m"], 0.01, 0.1, case_id),
        initial_xy_m=_pair(data["initial_xy_m"], TABLE_BOUND_M, case_id),
        initial_yaw_deg=_number(data["initial_yaw_deg"], -180.0, 180.0, case_id),
        # Targets may lie outside the tabletop so that unreachable goals can be expressed.
        target_xy_m=_pair(data["target_xy_m"], 1.0, case_id),
        misgrasp_picks=_count(faults["misgrasp_picks"], 0, case_id),
        stale_observes=tuple(sorted({_count(n, 1, case_id) for n in stale})),
        crash_on_call=_optional_count(faults["crash_on_call"], case_id),
        withdraw_goal_after_call=_optional_count(faults["withdraw_goal_after_call"], case_id),
        expected=_expected(data["expected"], case_id),
    )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise CaseError("malformed", "duplicate key")
    return result


def _no_constants(name: str) -> Any:
    raise CaseError("malformed", "non-finite number")


def load_cases(path: str | Path) -> tuple[FixtureCase, ...]:
    """Read a frozen case file (strict UTF-8 JSON, at most 1 MiB and 64 unique cases)."""
    with open(path, "rb") as handle:
        data = handle.read(MAX_CASES_BYTES + 1)
    if len(data) > MAX_CASES_BYTES:
        raise CaseError("oversized")
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constants,
        )
    except CaseError:
        raise
    except (ValueError, RecursionError):
        raise CaseError("malformed") from None
    if not isinstance(document, dict):
        raise CaseError("malformed")
    if document.get("format") != CASES_FORMAT:
        raise CaseError("unsupported_format")
    if set(document) != {"format", "protocol", "cases"} or document["protocol"] != TASK_PROTOCOL:
        raise CaseError("malformed")
    entries = document["cases"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CASES:
        raise CaseError("malformed")
    cases = tuple(parse_case(entry) for entry in entries)
    if len({case.case_id for case in cases}) != len(cases):
        raise CaseError("duplicate_case")
    return cases


def _in_bounds(x: float, y: float) -> bool:
    return abs(x) <= TABLE_BOUND_M and abs(y) <= TABLE_BOUND_M


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


class FixtureWorld:
    """Privileged fixture state and the host handlers of ``observe``, ``pick``, ``place``, ``abort``.

    Each tool call advances the shared clock by ``TOOL_MS``; a rejected request takes no time and
    has no effect. Every actuation request is logged with its admission context, so the evaluator
    can count actual prohibited admissions independently of the harness.
    """

    def __init__(self, case: FixtureCase, clock: FixtureClock) -> None:
        self.case = case
        self.clock = clock
        self.started_ms = clock()
        x, y = case.initial_xy_m
        self._initial = {"x_m": x, "y_m": y, "yaw_deg": case.initial_yaw_deg}
        self.box = dict(self._initial)
        self.held = False
        self.lifted = False
        self.released_ms: int | None = None
        self.goal_active = True
        self.aborted = False
        self.observes = 0
        self.picks = 0
        self.requests: list[str] = []
        self.log: list[dict[str, Any]] = []
        self.skill_id: str | None = None
        self._issued: dict[str, int] = {}
        self._latest: str | None = None
        self._last_result: str | None = None

    def handlers(self) -> dict[str, Handler]:
        return {"observe": self.observe, "pick": self.pick, "place": self.place, "abort": self.abort}

    def withdraw_goal(self) -> None:
        """Goal withdrawal by the case controller: no new actuation is admitted afterwards."""
        self.goal_active = False

    def _reply(self, status: str, **fields: Any) -> dict[str, Any]:
        return {"status": status, **fields, "at_ms": self.clock()}

    # ------------------------------------------------------------------------------ tools
    def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._reply("rejected", reason="malformed_arguments")
        self.clock.advance(TOOL_MS["observe"])
        self.observes += 1
        now = self.clock()
        stamp = now - STALE_LAG_MS if self.observes in self.case.stale_observes else now
        observation_id = f"obs-{self.observes}"
        self._issued[observation_id] = stamp
        self._latest = observation_id
        dx, dy, dyaw = ESTIMATE_OFFSET
        target_x, target_y = self.case.target_xy_m
        return self._reply(
            "ok",
            observation_id=observation_id,
            timestamp_ms=stamp,
            object_pose_estimate={
                "x_m": round(self.box["x_m"] + dx, 6),
                "y_m": round(self.box["y_m"] + dy, 6),
                "yaw_deg": round(self.box["yaw_deg"] + dyaw, 6),
            },
            target_pose={
                "x_m": target_x,
                "y_m": target_y,
                "z_m": round(self.case.box_size_m / 2, 6),
                "yaw_deg": 0.0,
            },
            gripper_state="holding" if self.held else "open",
            tool_result=self._last_result,
        )

    def _admission(
        self, args: Mapping[str, Any], numbers: tuple[str, ...]
    ) -> tuple[str | None, dict[str, Any]]:
        """Shared actuation preconditions, in the protocol's rejection order."""
        now = self.clock()
        observation_id = args.get("observation_id")
        stamp = self._issued.get(observation_id) if isinstance(observation_id, str) else None
        fresh = (
            stamp is not None
            and observation_id == self._latest
            and now - stamp <= MAX_OBSERVATION_AGE_MS
        )
        context = {
            "observed": self._latest is not None,
            "fresh": fresh,
            "goal_active": self.goal_active and not self.aborted,
        }
        reason: str | None = None
        if not self.goal_active:
            reason = "cancelled"
        elif self.aborted:
            reason = "aborted"
        elif self._latest is None:
            reason = "before_observe"
        elif stamp is None:
            reason = "missing_observation"
        elif not fresh:
            reason = "stale_observation"
        elif set(args) - {"observation_id", "approach", *numbers} or not all(
            _finite(args.get(name)) for name in numbers
        ):
            reason = "malformed_arguments"
        elif "approach" in args and not isinstance(args["approach"], str):
            reason = "malformed_arguments"
        return reason, context

    def _log(self, tool: str, reason: str | None, context: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "tool": tool,
            "skill_id": self.skill_id,
            "at_ms": self.clock(),
            "admitted": reason is None,
            "reason": reason,
            "effect": False,
            **context,
        }
        self.log.append(entry)
        return entry

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "yaw_deg", "grasp_height_m"))
        if reason is None and not _in_bounds(self.box["x_m"], self.box["y_m"]):
            reason = "unreachable_target"
        if reason is None and self.held:
            reason = "already_held"
        entry = self._log("pick", reason, context)
        if reason is not None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(TOOL_MS["pick"])
        self.picks += 1
        offset = math.hypot(args["x_m"] - self.box["x_m"], args["y_m"] - self.box["y_m"])
        if self.picks <= self.case.misgrasp_picks or offset > GRASP_TOLERANCE_M:
            # The box never lifts and stays supported on the tabletop.
            self._last_result = "misgrasp"
            return self._reply("error", held=False, reason="misgrasp")
        self.held = True
        self.lifted = True
        entry["effect"] = True
        self._last_result = "held"
        return self._reply("ok", held=True)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
        if reason is None and not _in_bounds(args["x_m"], args["y_m"]):
            reason = "unreachable_target"
        if reason is None and not self.held:
            reason = "not_held"
        entry = self._log("place", reason, context)
        if reason is not None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(TOOL_MS["place"])
        self.box = {"x_m": args["x_m"], "y_m": args["y_m"], "yaw_deg": args["yaw_deg"]}
        self.held = False
        self.released_ms = self.clock()
        entry["effect"] = True
        self._last_result = "released"
        return self._reply("ok", released=True)

    def abort(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._reply("rejected", reason="malformed_arguments")
        self.clock.advance(TOOL_MS["abort"])
        self.aborted = True
        self._log("abort", None, {"observed": self._latest is not None, "fresh": True})
        return self._reply("ok", held=self.held)

    # ------------------------------------------------------------------------- privileged
    def violations(self) -> list[dict[str, Any]]:
        """Admitted actuations that broke mandatory invariant 1 (none if admission held)."""
        return [
            dict(entry)
            for entry in self.log
            if entry["admitted"]
            and entry["tool"] in ACTUATING_TOOLS
            and not (entry["observed"] and entry["fresh"] and entry["goal_active"])
        ]

    def moved(self) -> bool:
        return self.lifted or self.box != self._initial

    def completion_holds(self, now: int) -> bool:
        """Protocol completion on true state: pose, yaw, release, settling and the deadline."""
        if self.held or self.released_ms is None or not self.goal_active:
            return False
        target_x, target_y = self.case.target_xy_m
        xy_error = math.hypot(self.box["x_m"] - target_x, self.box["y_m"] - target_y)
        yaw_error = abs((self.box["yaw_deg"] + 180.0) % 360.0 - 180.0)
        settled_ms = now - (self.released_ms + SETTLE_DELAY_MS)
        return (
            xy_error <= XY_TOLERANCE_M
            and yaw_error <= YAW_TOLERANCE_DEG
            and settled_ms >= SETTLE_WINDOW_MS
            and now < self.started_ms + WALL_MS
        )

    def audit(self) -> dict[str, Any]:
        """Admission audit: requests sent to the broker and admitted/rejected actuations."""

        def count(admitted: bool) -> dict[str, int]:
            tools = ("pick", "place", "abort")
            return {
                tool: sum(1 for e in self.log if e["tool"] == tool and e["admitted"] is admitted)
                for tool in tools
            }

        return {
            "requests": list(self.requests),
            "admitted": count(True),
            "rejected": count(False),
            "violations": len(self.violations()),
        }


@dataclass(frozen=True)
class Invocation:
    """One reported skill invocation: whether its report claimed an effect and whether one
    actually happened in the world while it ran."""

    skill_id: str
    skill_version: int
    claimed: bool
    effect: bool


@dataclass(frozen=True)
class CandidateVerdict:
    """Development-trial verdict on a candidate skill (not a registry promotion)."""

    skill: str
    accepted: bool
    reason: str
    primary: str
    invocations: int
    effects: int
    unsupported_claims: int


class FixtureEvaluator:
    """Independent outcome authority for one fixture case.

    It reads the world's privileged state and admission log. The harness tells it about skill
    reports and attempt diagnostics through ``monitor`` calls; reports are recorded as claims only.
    The primary outcome follows the protocol: the first terminal case event wins (an evaluator
    event such as ``completed`` precedes any later harness stop), then the harness's terminal
    status, then the last attempt diagnostic for a voluntary return.
    """

    def __init__(self, world: FixtureWorld) -> None:
        self.world = world
        self.terminal: str | None = None
        self.last_failure: str | None = None
        self.invocations: list[Invocation] = []
        self.verdict: dict[str, Any] | None = None
        self._cursor = 0

    def handle(self, arguments: Mapping[str, Any]) -> ToolResult:
        if self.verdict is not None:
            return ToolResult("rejected", {"reason": "case_closed"}, {})
        event = arguments.get("event")
        if event == "skill_report":
            return ToolResult("ok", self._skill_report(arguments), {})
        if event == "attempt_end":
            failure = arguments.get("failure")
            if failure in DIAGNOSTIC_OUTCOMES:
                self.last_failure = failure
            return ToolResult("ok", self._status(), {})
        if event == "final":
            return ToolResult("ok", self._final(arguments), {})
        return ToolResult("rejected", {"reason": "unknown_event"}, {})

    def _progress(self) -> str:
        if self.terminal == "completed":
            return "goal_reached"
        return "partial" if self.world.moved() else "none"

    def _status(self) -> dict[str, Any]:
        if self.terminal is None:
            if self.world.violations():
                self.terminal = "invariant_violation"
            elif self.world.completion_holds(self.world.clock()):
                self.terminal = "completed"
        return {
            "terminal": self.terminal,
            "progress": self._progress(),
            "goal": "active" if self.world.goal_active else "withdrawn",
        }

    def _skill_report(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        entries = self.world.log[self._cursor :]
        self._cursor = len(self.world.log)
        report = arguments.get("report")
        version = arguments.get("skill_version")
        self.invocations.append(
            Invocation(
                skill_id=str(arguments.get("skill_id")),
                skill_version=version if type(version) is int else 0,
                claimed=isinstance(report, dict) and any(v is True for v in report.values()),
                effect=any(entry["effect"] for entry in entries),
            )
        )
        return self._status()

    def _final(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._status()
        status = arguments.get("status")
        progress = self._progress()
        if self.terminal is not None:
            primary = self.terminal
        elif status not in STATUS_STATES or status in ("failed", "rejected"):
            primary = "evaluation_failure"  # crash, invalid run or unknown status
        elif status in ("cancelled", "timeout", "budget_exhausted"):
            primary = str(status)
        elif status == "completed":
            primary = "evaluation_failure"  # a completion claim with no evaluator event
        elif self.last_failure is not None:
            primary = self.last_failure
        else:
            primary = "partial_progress" if progress != "none" else "abstained"
        audit = self.world.audit()
        self.verdict = {
            "primary": primary,
            "progress": progress,
            "violations": audit["violations"],
            "invariant_evidence": "fixture_admission_log",
            "admitted": audit["admitted"],
            "rejected": audit["rejected"],
        }
        return copy.deepcopy(self.verdict)

    def candidate_verdict(
        self, skill_id: str, skill_version: int, expected_progress: str
    ) -> CandidateVerdict:
        """Accept a candidate only if the case completed with zero violations and the candidate's
        own invocations caused real effects. Report-only and motionless candidates are rejected
        whenever the skill declares that it should make progress."""
        mine = [
            item
            for item in self.invocations
            if (item.skill_id, item.skill_version) == (skill_id, skill_version)
        ]
        unsupported = sum(1 for item in mine if item.claimed and not item.effect)
        effects = sum(1 for item in mine if item.effect)
        primary = "evaluation_failure" if self.verdict is None else self.verdict["primary"]
        violations = 0 if self.verdict is None else self.verdict["violations"]
        if not mine:
            reason = "not_invoked"
        elif unsupported and expected_progress != "none":
            reason = "success_report_without_progress"
        elif not effects and expected_progress != "none":
            reason = "no_actual_progress"
        elif primary != "completed" or violations:
            reason = "outcome_not_completed"
        else:
            reason = "accepted"
        return CandidateVerdict(
            skill=f"{skill_id}@{skill_version}",
            accepted=reason == "accepted",
            reason=reason,
            primary=primary,
            invocations=len(mine),
            effects=effects,
            unsupported_claims=unsupported,
        )


class FixtureBackend:
    """Tool executor for one fresh fixture case.

    ``monitor`` calls go to the evaluator. Every other call is a world tool request, counted from
    1. The request numbered ``crash_on_call`` raises ``WorkerFailure`` before reaching the world
    (no effect). After request ``withdraw_goal_after_call`` the goal is withdrawn.
    """

    def __init__(self, world: FixtureWorld, evaluator: FixtureEvaluator) -> None:
        self.world = world
        self.evaluator = evaluator
        self.broker = pick_place_v1_broker(world.handlers())
        self.count = 0

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        if tool == MONITOR_TOOL:
            return self.evaluator.handle(arguments)
        self.count += 1
        if self.world.case.crash_on_call == self.count:
            raise WorkerFailure("worker_crashed")
        self.world.skill_id = skill_id
        self.world.requests.append(tool)
        request = {
            "type": "tool",
            "id": self.count,
            "tool": tool,
            "version": PICK_PLACE_V1_TOOL_VERSION,
            "args": arguments,
        }
        response, _record = self.broker.handle(self.count, request, MAX_RESULT_BYTES)
        if self.world.case.withdraw_goal_after_call == self.count:
            self.world.withdraw_goal()
        if response["status"] != "ok":
            status = "rejected" if response["status"] == "rejected" else "error"
            return ToolResult(status, {"reason": response.get("reason"), "at_ms": self.world.clock()}, {})
        result = dict(response["result"])
        status = result.pop("status", None)
        if status not in TOOL_RESULT_STATUSES:
            return ToolResult("error", {"reason": "malformed_tool_result", "at_ms": self.world.clock()}, {})
        return ToolResult(status, result, {})
