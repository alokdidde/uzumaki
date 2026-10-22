"""Offline rule-based execution baseline for ``pick-place-v1`` (issue 08).

One case runs goal -> observation -> eligibility -> rule-based selection -> bounded skill
execution -> attempts and retries -> independent verification -> final event bundle, with no model
provider. The pieces are the accepted predecessors:

- ``uzumaki.registry``: the selector may choose only skills in the current ``eligible_skills`` view
  of one immutable snapshot, pinned for the run, whose required observations are present. A
  development trial may additionally name one registered ``candidate`` or ``validated`` skill.
- ``uzumaki.supervisor.BoundedRun``: protocol budgets (3 attempts, 120 s wall on the fixture clock,
  0 model calls, 40 tool steps), lifecycle, cancellation and fencing. Every world tool call goes
  through it; a fourth attempt is refused as ``budget_exhausted``.
- ``uzumaki.evidence.EventRecorder``: observations, selector decisions, every tool and monitor call
  with its result, the evaluator's outcome and the terminal state in one checksum-chained bundle.
- ``uzumaki.fixture_env``: the world decides admission and effects; the evaluator alone decides
  outcomes. The harness reports skill outputs to the evaluator as claims only.

The selector is ``rule-selector-v1`` from ``docs/protocol/task-v1.json``. It is recorded under
the ``planner`` role with that ID; it is deterministic code, not a model. Skill bodies are
hand-authored host code keyed by ``skill_id@version`` and may call only the tools their artifact
permits. They are trusted fixture code, not generated candidates, so they do not run in the
restricted worker.

Attempt rules follow the protocol: the first observe of an attempt starts it; a missing or stale
observation, a failed or rejected pick/place, or an unfinished settling window ends it. A
failure in the chosen skill's ``stop_on`` ends the case (status ``blocked``); any other failure
is retried from the state actually reached. After a successful place the harness observes up to
``SETTLE_OBSERVES`` times while the evaluator checks the settling window.

The same driver runs the two-effector ``handover-v1`` variant (``protocol=HANDOVER_PROTOCOL``):
its eligible skills are those that declare that protocol, the selector also sees which role holds
the object (``holder``), and ``handover-selector-v1`` picks with the giver, hands over, then places
with the taker. ``hand_over`` is its one extra skill body. The mobile ``fetch-v1`` variant adds
``go_to``: ``fetch-selector-v1`` moves the body until the object is within reach (observation
field ``within_reach``), picks, moves until the target is within reach, then places.

``replay_case`` re-drives the same logic against a recorded bundle (recorded-decision replay: the
recorded selector outputs and tool/monitor results) and compares semantic outcomes; it never runs
the world or the evaluator again.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from uzumaki.budgets import Budgets
from uzumaki.evidence import (
    ID_PATTERN,
    DecisionSource,
    EventRecorder,
    ToolExecutor,
    ToolResult,
    validate_bundle,
    write_bundle,
)
from uzumaki.faults import WorkerFailure
from uzumaki.fixture_env import (
    FETCH_PROTOCOL,
    HANDOVER_PROTOCOL,
    MAX_OBSERVATION_AGE_MS,
    MONITOR_TOOL,
    OBSERVATION_SOURCE,
    TASK_PROTOCOL,
    CandidateVerdict,
    FixtureBackend,
    FixtureCase,
    FixtureClock,
    FixtureEvaluator,
    FixtureWorld,
)
from uzumaki.registry import (
    PICK_PLACE_V1_OUTCOMES,
    PROGRESS,
    PinnedSkill,
    RegistrySnapshot,
    Skill,
    SkillError,
)
from uzumaki.replay import ReplayDivergence, ReplayMode, ReplayReport, replay, semantic_outcome
from uzumaki.supervisor import BoundedRun, ModelProvider, RunReport, RunStopped

BASELINE_ID = "rule-selector-v1"
HANDOVER_SELECTOR_ID = "handover-selector-v1"
FETCH_SELECTOR_ID = "fetch-selector-v1"
TRIAL_SELECTOR_ID = "trial-selector-v1"
PLANNER_ROLE = "planner"
#: Observes after a successful place while the evaluator checks the 0.5 s settling window. With
#: 100 ms fixture observes, completion can hold from the seventh; the eighth is the last chance.
SETTLE_OBSERVES = 8
#: Selector decisions within one attempt (the baseline needs two: pick, then place).
MAX_DECISIONS_PER_ATTEMPT = 4
#: Protocol budgets (3 attempts, 120 s, no model calls) plus fixture ceilings on tool steps and
#: output bytes. Steps cover three full attempts (11 tool calls each) and one abort.
BASELINE_BUDGETS = Budgets(
    wall_ms=120_000,
    model_calls=0,
    steps=40,
    retries=0,
    max_output_bytes=4096,
    max_total_output_bytes=65536,
    attempts=3,
)
#: Budgets for a model planner (``uzumaki.planner``): the protocol's 20 model calls per case,
#: retries of invalid or transient model answers within that count, and room for model outputs.
MODEL_BUDGETS = Budgets(
    wall_ms=120_000,
    model_calls=20,
    steps=40,
    retries=2,
    max_output_bytes=8192,
    max_total_output_bytes=262_144,
    attempts=3,
)
#: Fixed names of the frozen per-case correctness checks (``correctness_failures``).
CORRECTNESS_CHECKS = (
    "evidence_valid",
    "primary",
    "progress",
    "state",
    "status",
    "attempts",
    "admitted",
    "violations",
    "model_calls",
    "bundle_outcome",
    "bundle_state",
)

#: Protocol ``request_rejections``: world rejection reason -> attempt diagnostic. Goal withdrawal
#: and abort are cancellation. Everything else is the catch-all ``tool_rejected``.
_REJECTION_OUTCOMES: Mapping[str, str] = MappingProxyType(
    {
        "before_observe": "missing_observation",
        "missing_observation": "missing_observation",
        "stale_observation": "missing_observation",
        "ambiguous_goal": "ambiguous_goal",
        "unreachable_target": "unreachable_target",
        "cancelled": "cancelled",
        "aborted": "cancelled",
    }
)
_POSE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "object_pose_estimate": ("x_m", "y_m", "yaw_deg"),
        "target_pose": ("x_m", "y_m", "z_m", "yaw_deg"),
    }
)


def rejection_outcome(reason: Any) -> str:
    """Map a pick/place rejection reason to its protocol attempt diagnostic."""
    if isinstance(reason, str):
        return _REJECTION_OUTCOMES.get(reason, "tool_rejected")
    return "tool_rejected"


class ToolNotPermitted(Exception):
    """A skill body asked for a tool its artifact does not permit. Nothing was sent."""


@dataclass(frozen=True)
class SkillResult:
    """What a skill body returns: outputs (reported to the evaluator as a claim) and the attempt
    diagnostic, if the skill failed."""

    outputs: dict[str, Any]
    failure: str | None = None


SkillBody = Callable[["SkillContext", Mapping[str, Any]], SkillResult]


class SkillContext:
    """Everything a skill body may use: the latest fresh observation and its permitted tools."""

    def __init__(self, driver: _Driver, skill: Skill, observation: Mapping[str, Any]) -> None:
        self._driver = driver
        self._permitted = frozenset(skill.tool_permissions)
        self.skill = skill
        self.observation: dict[str, Any] = copy.deepcopy(dict(observation))

    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        """One bounded tool call by this skill (observations go through ``observe``)."""
        if tool == "observe" or tool not in self._permitted:
            raise ToolNotPermitted(tool)
        return self._driver.run.call_tool(tool, arguments, skill_id=self.skill.skill_id)

    def observe(self) -> dict[str, Any] | None:
        """Observe again; returns the observation if valid and fresh, otherwise ``None``."""
        if "observe" not in self._permitted:
            raise ToolNotPermitted("observe")
        observation = self._driver.observe(self.skill.skill_id)
        if observation is not None:
            self.observation = observation
        return observation

    def fail(self, detail: str) -> NoReturn:
        """End the run as ``failed`` because this skill's implementation broke (for example a
        generated program that crashed in the restricted worker). ``detail`` is a fixed code."""
        self._driver._stop(detail)


def pick_box(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``pick-box@1``: pick at the latest object pose estimate with the given grasp height."""
    estimate = ctx.observation["object_pose_estimate"]
    result = ctx.call(
        "pick",
        {
            "observation_id": ctx.observation["observation_id"],
            "x_m": estimate["x_m"],
            "y_m": estimate["y_m"],
            "yaw_deg": estimate["yaw_deg"],
            "grasp_height_m": parameters["grasp_height_m"],
        },
    )
    if result.status == "ok" and result.result.get("held") is True:
        return SkillResult({"held": True})
    if result.status == "rejected":
        return SkillResult({"held": False}, rejection_outcome(result.result.get("reason")))
    failure = "misgrasp" if result.result.get("reason") == "misgrasp" else "tool_rejected"
    return SkillResult({"held": False}, failure)


def place_box(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``place-box@1``: place the held box at the observed target pose and release it."""
    target = ctx.observation["target_pose"]
    result = ctx.call(
        "place",
        {
            "observation_id": ctx.observation["observation_id"],
            "x_m": target["x_m"],
            "y_m": target["y_m"],
            "z_m": target["z_m"],
            "yaw_deg": target["yaw_deg"],
            "approach": parameters.get("approach", "vertical"),
        },
    )
    if result.status == "ok" and result.result.get("released") is True:
        return SkillResult({"released": True})
    if result.status == "rejected":
        return SkillResult({"released": False}, rejection_outcome(result.result.get("reason")))
    return SkillResult({"released": False}, "placement_failure")


def hand_over(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``hand-over@1`` (``handover-v1``): pass the held object from the giver to the taker."""
    result = ctx.call("handover", {"observation_id": ctx.observation["observation_id"]})
    if result.status == "ok" and result.result.get("holder") == "taker":
        return SkillResult({"handed_over": True})
    if result.status == "rejected":
        return SkillResult({"handed_over": False}, rejection_outcome(result.result.get("reason")))
    # The taker did not end up holding it: a failed grasp, whatever the giver did.
    return SkillResult({"handed_over": False}, "misgrasp")


def go_to(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``go-to@1`` (``fetch-v1``): move the body until the observed object (``to: object``) or
    target (``to: target``) is within reach. A move that ends anywhere else is a failed base
    placement (``placement_failure``)."""
    pose = ctx.observation[
        "object_pose_estimate" if parameters["to"] == "object" else "target_pose"
    ]
    result = ctx.call(
        "go_to",
        {
            "observation_id": ctx.observation["observation_id"],
            "x_m": pose["x_m"],
            "y_m": pose["y_m"],
        },
    )
    if result.status == "ok":
        return SkillResult({"arrived": True})
    if result.status == "rejected":
        return SkillResult({"arrived": False}, rejection_outcome(result.result.get("reason")))
    return SkillResult({"arrived": False}, "placement_failure")


def pick_and_place(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``pick-and-place@1``: ``pick-box``, a fresh observe, then ``place-box``."""
    picked = pick_box(ctx, parameters)
    if picked.failure is not None:
        return SkillResult({"released": False}, picked.failure)
    if ctx.observe() is None:
        return SkillResult({"released": False}, "missing_observation")
    return place_box(ctx, {"approach": "vertical"})


#: Hand-authored bodies of the validated skills in tests/fixtures/skills_pick_place_v1.json.
BASELINE_SKILL_BODIES: Mapping[str, SkillBody] = MappingProxyType(
    {
        "pick-box@1": pick_box,
        "place-box@1": place_box,
        "pick-and-place@1": pick_and_place,
        "hand-over@1": hand_over,
        "go-to@1": go_to,
    }
)
#: Observation fields the selector sees, per protocol (besides ``observation_id``).
DECISION_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        TASK_PROTOCOL: ("gripper_state", "target_pose"),
        HANDOVER_PROTOCOL: ("gripper_state", "holder", "target_pose"),
        FETCH_PROTOCOL: ("gripper_state", "within_reach", "target_pose"),
    }
)


def _abstain(reason: str) -> dict[str, Any]:
    return {"action": "abstain", "reason": reason}


def _select(entry: Mapping[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "select",
        "skill_id": entry["skill_id"],
        "skill_version": entry["skill_version"],
        "parameters": parameters,
    }


def _grasp_height(request: Mapping[str, Any]) -> float | None:
    target = request["observation"].get("target_pose")
    if not isinstance(target, dict):
        return None
    height = target.get("z_m")
    return float(height) if isinstance(height, int | float) else None


class RuleSelector:
    """``rule-selector-v1``: place when the gripper holds the box, otherwise pick.

    It chooses only among the skills listed in the request (the current eligible set) and abstains
    when the skill it needs is not there. Grasp height is the target's ``z_m`` (half the box size).
    """

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        eligible = {entry["skill_id"]: entry for entry in request["eligible"]}
        if request["observation"]["gripper_state"] == "holding":
            entry = eligible.get("place-box")
            if entry is None:
                return _abstain("no_eligible_skill")
            return _select(entry, {"approach": "vertical"})
        entry = eligible.get("pick-box")
        height = _grasp_height(request)
        if entry is None or height is None:
            return _abstain("no_eligible_skill")
        return _select(entry, {"grasp_height_m": height})


class HandoverSelector:
    """``handover-selector-v1``: pick with the giver, hand over, then place with the taker.

    Like ``rule-selector-v1`` it chooses only among the listed skills and abstains otherwise."""

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        eligible = {entry["skill_id"]: entry for entry in request["eligible"]}
        holder = request["observation"].get("holder")
        if request["observation"]["gripper_state"] == "holding":
            skill, parameters = (
                ("hand-over", {}) if holder == "giver" else ("place-box", {"approach": "vertical"})
            )
            entry = eligible.get(skill)
            return _abstain("no_eligible_skill") if entry is None else _select(entry, parameters)
        entry = eligible.get("pick-box")
        height = _grasp_height(request)
        if entry is None or height is None:
            return _abstain("no_eligible_skill")
        return _select(entry, {"grasp_height_m": height})


class FetchSelector:
    """``fetch-selector-v1``: move to the object, pick, move to the target, place.

    It reads ``within_reach`` and, like ``rule-selector-v1``, chooses only among the listed skills
    and abstains otherwise."""

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        eligible = {entry["skill_id"]: entry for entry in request["eligible"]}
        reach = request["observation"].get("within_reach")
        if not isinstance(reach, dict):
            return _abstain("no_reach_information")
        holding = request["observation"]["gripper_state"] == "holding"
        here = reach.get("target" if holding else "object") is True
        if not here:
            entry = eligible.get("go-to")
            to = "target" if holding else "object"
            return _abstain("no_eligible_skill") if entry is None else _select(entry, {"to": to})
        if holding:
            entry = eligible.get("place-box")
            return (
                _abstain("no_eligible_skill")
                if entry is None
                else _select(entry, {"approach": "vertical"})
            )
        entry = eligible.get("pick-box")
        height = _grasp_height(request)
        if entry is None or height is None:
            return _abstain("no_eligible_skill")
        return _select(entry, {"grasp_height_m": height})


#: Protocol -> its rule selector and that selector's recorded ID.
RULE_SELECTORS: Mapping[str, tuple[str, Callable[[], DecisionSource]]] = MappingProxyType(
    {
        TASK_PROTOCOL: (BASELINE_ID, lambda: RuleSelector()),
        HANDOVER_PROTOCOL: (HANDOVER_SELECTOR_ID, lambda: HandoverSelector()),
        FETCH_PROTOCOL: (FETCH_SELECTOR_ID, lambda: FetchSelector()),
    }
)


class TrialSelector:
    """Development-trial selector: always picks the declared trial candidate."""

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        trial = request.get("trial_candidate")
        if not isinstance(trial, dict):
            return _abstain("candidate_not_applicable")
        parameters: dict[str, Any] = {}
        if "grasp_height_m" in trial["inputs"]:
            height = _grasp_height(request)
            if height is None:
                return _abstain("candidate_not_applicable")
            parameters["grasp_height_m"] = height
        return _select(trial, parameters)


@dataclass(frozen=True)
class CasePlan:
    """Everything that must be identical between a fresh run and its replay."""

    run_id: str
    task_id: str
    snapshot: RegistrySnapshot
    pins: Mapping[str, PinnedSkill]
    trial: Skill | None
    bodies: Mapping[str, SkillBody]
    planner_id: str | None = None
    protocol: str = TASK_PROTOCOL

    @property
    def selector_id(self) -> str:
        if self.planner_id is not None:
            return self.planner_id
        return RULE_SELECTORS[self.protocol][0] if self.trial is None else TRIAL_SELECTOR_ID

    @property
    def candidate_id(self) -> str | None:
        return None if self.trial is None else f"{self.trial.skill_id}:{self.trial.skill_version}"

    @property
    def skill_versions(self) -> dict[str, str]:
        skills: list[Skill] = []
        for pin in self.pins.values():
            skills.append(pin.skill)
            skills.extend(pin.dependencies)
        if self.trial is not None:
            skills.append(self.trial)
        return {skill.skill_id: f"{skill.skill_version}:{skill.sha256}" for skill in skills}

    @property
    def model_versions(self) -> dict[str, str]:
        return {PLANNER_ROLE: self.selector_id}


def plan_case(
    case_id: str,
    snapshot: RegistrySnapshot,
    *,
    candidate: tuple[str, int] | None = None,
    bodies: Mapping[str, SkillBody] | None = None,
    planner_id: str | None = None,
    protocol: str = TASK_PROTOCOL,
) -> CasePlan:
    """Pin every eligible validated skill for one run and check the optional trial candidate.

    A trial candidate must be registered as ``candidate`` or ``validated``, apply to
    ``protocol``, verify, and have a resolvable validated dependency closure and a body.
    Trials never change registry state. ``planner_id`` names a non-rule decision source (a model
    planner's recorded model version); it must be the same for a run and its replay.
    """
    if not isinstance(case_id, str) or not ID_PATTERN.match(case_id):
        raise ValueError("case_id is not a valid identifier")
    if protocol not in RULE_SELECTORS:
        raise ValueError("unknown task protocol")
    run_id = f"{'baseline' if candidate is None else 'trial'}.{case_id}"
    pins = {
        skill.skill_id: snapshot.pin(skill.skill_id, skill.skill_version, run_id=run_id)
        for skill in snapshot.eligible(protocol)
    }
    implementations = dict(BASELINE_SKILL_BODIES)
    implementations.update(bodies or {})
    trial: Skill | None = None
    if candidate is not None:
        skill_id, skill_version = candidate
        trial = snapshot.get(skill_id, skill_version)
        if snapshot.state(skill_id, skill_version) not in ("candidate", "validated"):
            raise ValueError("trial candidate must be in state candidate or validated")
        if protocol not in trial.applicability["task_protocols"]:
            raise ValueError(f"trial candidate does not apply to {protocol}")
        trial.verify()
        snapshot.resolve_dependencies(trial)
        if trial.label not in implementations:
            raise ValueError("trial candidate has no implementation")
    return CasePlan(
        run_id=run_id,
        task_id=case_id,
        snapshot=snapshot,
        pins=MappingProxyType(pins),
        trial=trial,
        bodies=MappingProxyType(implementations),
        planner_id=planner_id,
        protocol=protocol,
    )


class _RecordingTools:
    """Records every world call and its result in the bundle around the real or recorded executor.

    A ``WorkerFailure`` is recorded as an error result marked ``raised`` and raised again, so a
    replay of that result fails the run exactly like the original crash. A replay divergence is
    remembered, because ``BoundedRun`` turns executor exceptions into a failed run.
    """

    def __init__(self, recorder: EventRecorder, inner: ToolExecutor) -> None:
        self._recorder = recorder
        self._inner = inner
        self.diverged: ReplayDivergence | None = None

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        self._recorder.tool_call(call_id, tool, arguments, skill_id)
        try:
            result = self._inner.execute(call_id, tool, copy.deepcopy(arguments), skill_id)
        except ReplayDivergence as error:
            self.diverged = error
            raise
        except WorkerFailure:
            crash = {"raised": "worker_failure"}
            result = ToolResult("error", {"reason": "worker_crashed"}, crash)
        self._recorder.tool_result(call_id, result.status, result.result, result.diagnostics)
        if result.diagnostics.get("raised") == "worker_failure":
            raise WorkerFailure("worker_crashed")
        return result


def _listing(skill: Skill) -> dict[str, Any]:
    return {
        "skill_id": skill.skill_id,
        "skill_version": skill.skill_version,
        "content_sha256": skill.sha256,
    }


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _valid_observation(observation: Any) -> bool:
    """Declared fields have the declared types; an absent pose stays absent (``None``)."""
    if not isinstance(observation, dict):
        return False
    observation_id = observation.get("observation_id")
    if not isinstance(observation_id, str) or not ID_PATTERN.match(observation_id):
        return False
    stamp, at = observation.get("timestamp_ms"), observation.get("at_ms")
    if type(stamp) is not int or type(at) is not int or stamp < 0:
        return False
    if observation.get("gripper_state") not in ("open", "holding"):
        return False
    for name, keys in _POSE_FIELDS.items():
        pose = observation.get(name)
        if pose is None:
            continue
        if not isinstance(pose, dict) or not all(_finite(pose.get(key)) for key in keys):
            return False
    return True


@dataclass(frozen=True)
class _AttemptEnd:
    status: str | None = None  # a voluntary harness status that ends the case
    failure: str | None = None  # an attempt diagnostic
    skill: Skill | None = None  # the skill whose failure semantics apply


class _Counter:
    """Replay clock: no world, so time is a counter (wall time is never reached)."""

    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> int:
        self.t += 1
        return self.t


class _Driver:
    """Deterministic harness logic for one case; identical for fresh runs and replays."""

    def __init__(
        self,
        plan: CasePlan,
        recorder: EventRecorder,
        decisions: DecisionSource,
        tools: ToolExecutor,
        clock: Callable[[], int],
        budgets: Budgets = BASELINE_BUDGETS,
        provider: ModelProvider | None = None,
    ) -> None:
        self.plan = plan
        self.budgets = budgets
        self.provider = provider
        self.recorder = recorder
        self.decisions = decisions
        self.tools = tools
        self.clock = clock
        self.world_tools = _RecordingTools(recorder, tools)
        self._run: BoundedRun | None = None
        self._monitors = 0
        self._decisions = 0
        self._views = 0

    @property
    def run(self) -> BoundedRun:
        assert self._run is not None
        return self._run

    # ------------------------------------------------------------------------------ run
    def execute(self) -> tuple[dict[str, Any], RunReport]:
        self._run = BoundedRun(
            run_id=self.plan.run_id,
            budgets=self.budgets,
            provider=self.provider,
            tools=self.world_tools,
            clock=self.clock,
        )
        bind = getattr(self.decisions, "bind", None)
        if callable(bind):  # a model planner makes its model calls through this run
            bind(self._run)
        report = self._run.execute(self._logic)
        if self.world_tools.diverged is not None:
            raise self.world_tools.diverged
        final = self._monitor_call({"event": "final", "status": report.status})
        primary, progress = "evaluation_failure", "none"
        if final.status == "ok":
            claimed = (final.result.get("primary"), final.result.get("progress"))
            if claimed[0] in PICK_PLACE_V1_OUTCOMES and claimed[1] in PROGRESS:
                primary, progress = str(claimed[0]), str(claimed[1])
        self.recorder.outcome(primary, progress)
        return self.recorder.finish(report.state), report

    def _logic(self, run: BoundedRun) -> str:
        while True:
            end = self._attempt()
            if end.status is not None:
                return end.status
            assert end.failure is not None
            self._monitor("attempt_end", attempt=run.attempt_id, failure=end.failure)
            if end.skill is not None and end.failure in end.skill.failure["stop_on"]:
                return "blocked"
            run.new_attempt()  # refused after the third attempt: budget_exhausted

    def _attempt(self) -> _AttemptEnd:
        observation = self.observe(None)  # the attempt-starting observe
        if observation is None:
            return _AttemptEnd(failure="missing_observation")
        for _ in range(MAX_DECISIONS_PER_ATTEMPT):
            chosen = self._decide(observation)
            if chosen is None:
                voluntary = "partial_progress" if self.run.progress == "partial" else "abstained"
                return _AttemptEnd(status=voluntary)
            skill, parameters = chosen
            result = self._invoke(skill, parameters, observation)
            status = self._monitor(
                "skill_report",
                skill_id=skill.skill_id,
                skill_version=skill.skill_version,
                report=result.outputs,
            )
            if status.get("terminal") == "completed":
                return _AttemptEnd(status="completed")
            if result.failure == "cancelled":
                self._cancel()
            if result.failure is not None:
                return _AttemptEnd(failure=result.failure, skill=skill)
            if skill.completion["expected_progress"] == "goal_reached":
                if self._settle():
                    return _AttemptEnd(status="completed")
                return _AttemptEnd(failure="placement_failure", skill=skill)
            observation = self.observe(None)
            if observation is None:
                return _AttemptEnd(failure="missing_observation")
        self._stop("decision_limit")

    def _settle(self) -> bool:
        for _ in range(SETTLE_OBSERVES):
            self.observe(None)
            if self._monitor("status").get("terminal") == "completed":
                return True
        return False

    # ------------------------------------------------------------------------ helpers
    def _stop(self, detail: str) -> NoReturn:
        self.run.fail(detail)
        report = self.run.report()
        raise RunStopped(report.state, report.status, report.detail)

    def _cancel(self) -> NoReturn:
        """Goal withdrawn: abort (no rollback claim), then cancel; no further actuation."""
        self.run.call_tool("abort", {})
        self.run.cancel("goal_withdrawn")
        report = self.run.report()
        raise RunStopped(report.state, report.status, report.detail)

    def _monitor_call(self, arguments: dict[str, Any]) -> ToolResult:
        """One evaluator call, recorded like a tool call. It is not a world step: it actuates
        nothing, so it is outside ``BoundedRun`` and still answers after the run stopped."""
        self._monitors += 1
        try:
            return self.recorder.call_tool(
                self.tools, f"m{self._monitors}", MONITOR_TOOL, arguments, None
            )
        except ReplayDivergence as error:
            self.world_tools.diverged = error
            raise

    def _monitor(self, event: str, **fields: Any) -> dict[str, Any]:
        """Ask the independent evaluator; relay its progress, terminal events and goal channel."""
        result = self._monitor_call({"event": event, **fields})
        if result.status != "ok":
            self._stop("evaluator_unavailable")
        status = result.result
        if status.get("progress") in PROGRESS:
            self.run.record_progress(str(status["progress"]), "evaluator")
        terminal = status.get("terminal")
        if terminal is not None and terminal != "completed":
            self._stop("evaluator_terminal")
        if status.get("goal") == "withdrawn":
            self._cancel()
        return status

    def observe(self, skill_id: str | None) -> dict[str, Any] | None:
        """Observe and record the observation; ``None`` if missing, malformed or stale."""
        result = self.run.call_tool("observe", {}, skill_id)
        observation = result.result
        if result.status != "ok" or not _valid_observation(observation):
            return None
        self.recorder.observation(
            observation["observation_id"],
            OBSERVATION_SOURCE,
            observation["timestamp_ms"],
            observation,
        )
        age = observation["at_ms"] - observation["timestamp_ms"]
        if not 0 <= age <= MAX_OBSERVATION_AGE_MS:
            return None
        return copy.deepcopy(observation)

    def _applicable(self, skill: Skill, observation: Mapping[str, Any]) -> bool:
        applicability = skill.applicability
        if self.plan.protocol not in applicability["task_protocols"]:
            return False
        required = applicability["required_observations"]
        return all(observation.get(name) is not None for name in required)

    def _decide(self, observation: Mapping[str, Any]) -> tuple[Skill, dict[str, Any]] | None:
        """Eligible view, recorded selector decision, validated choice (``None``: abstain)."""
        self._views += 1
        view = self.plan.snapshot.eligible_skills(
            eligible_set_id=f"e{self._views}",
            based_on_observation_id=observation["observation_id"],
            computed_at_ms=observation["at_ms"],
            task_protocol=self.plan.protocol,
        )["payload"]
        allowed: dict[tuple[str, int], Skill] = {}
        for entry in view["skills"]:
            pin = self.plan.pins.get(entry["skill_id"])
            if pin is None or pin.skill_version != entry["skill_version"]:
                continue
            if pin.sha256 == entry["content_sha256"] and self._applicable(pin.skill, observation):
                allowed[pin.skill.key] = pin.skill
        listed = [_listing(skill) for skill in sorted(allowed.values(), key=lambda s: s.key)]
        trial: dict[str, Any] | None = None
        if self.plan.trial is not None and self._applicable(self.plan.trial, observation):
            candidate = self.plan.trial
            allowed[candidate.key] = candidate
            trial = {**_listing(candidate), "inputs": sorted(candidate.inputs["properties"])}
        request = {
            "task_protocol": self.plan.protocol,
            "eligible_set_id": view["eligible_set_id"],
            "registry_version": view["registry_version"],
            "observation": {
                "observation_id": observation["observation_id"],
                **{name: observation.get(name) for name in DECISION_FIELDS[self.plan.protocol]},
            },
            "eligible": listed,
            "trial_candidate": trial,
        }
        self._decisions += 1
        output = self.recorder.decide(self.decisions, f"d{self._decisions}", PLANNER_ROLE, request)
        action = output.get("action")
        if action == "abstain":
            return None
        skill_id, version = output.get("skill_id"), output.get("skill_version")
        if action != "select" or not isinstance(skill_id, str) or type(version) is not int:
            self._stop("invalid_decision")
        skill = allowed.get((skill_id, version))
        if skill is None or skill.label not in self.plan.bodies:
            self._stop("invalid_decision")
        parameters = output.get("parameters")
        try:
            skill.verify()
            skill.check_parameters(parameters)
        except SkillError:
            self._stop("invalid_decision")
        assert isinstance(parameters, dict)
        return skill, dict(parameters)

    def _invoke(
        self, skill: Skill, parameters: dict[str, Any], observation: Mapping[str, Any]
    ) -> SkillResult:
        context = SkillContext(self, skill, observation)
        try:
            result = self.plan.bodies[skill.label](context, parameters)
        except ToolNotPermitted:
            return SkillResult({}, "tool_rejected")
        if not isinstance(result, SkillResult) or not isinstance(result.outputs, dict):
            self._stop("invalid_skill_output")
        if result.failure is not None:
            if result.failure not in PICK_PLACE_V1_OUTCOMES or result.failure == "completed":
                self._stop("invalid_skill_output")
            return result
        try:
            skill.check_result(result.outputs)
        except SkillError:
            self._stop("invalid_skill_output")
        return result


@dataclass(frozen=True)
class CaseRun:
    """A fresh fixture execution: the bundle plus host-side records the bundle does not carry."""

    case_id: str
    bundle: dict[str, Any]
    report: RunReport
    verdict: dict[str, Any] | None
    audit: dict[str, Any]
    candidate: CandidateVerdict | None


@dataclass(frozen=True)
class CaseEnvironment:
    """The world side of one case: shared clock, privileged world and independent evaluator.

    ``world`` and ``evaluator`` are the fixture classes or subclasses with the same contract, such
    as the MuJoCo world in ``uzumaki.sim``."""

    clock: FixtureClock
    world: FixtureWorld
    evaluator: FixtureEvaluator


def fixture_environment(case: FixtureCase) -> CaseEnvironment:
    """A fresh scripted fixture world and evaluator for one case."""
    clock = FixtureClock()
    world = FixtureWorld(case, clock)
    return CaseEnvironment(clock, world, FixtureEvaluator(world))


def run_case(
    case: FixtureCase,
    snapshot: RegistrySnapshot,
    environment: Callable[[FixtureCase], CaseEnvironment],
    *,
    candidate: tuple[str, int] | None = None,
    bodies: Mapping[str, SkillBody] | None = None,
    selector: DecisionSource | None = None,
    planner_id: str | None = None,
    budgets: Budgets = BASELINE_BUDGETS,
    provider: ModelProvider | None = None,
    protocol: str = TASK_PROTOCOL,
) -> CaseRun:
    """Execute one case of ``protocol`` against a fresh environment built by ``environment``.

    ``selector`` replaces the rule (or trial) selector: a model planner (with ``planner_id``, model
    ``budgets`` and its ``provider``), or a test double for decision validation.
    """
    plan = plan_case(
        case.case_id,
        snapshot,
        candidate=candidate,
        bodies=bodies,
        planner_id=planner_id,
        protocol=protocol,
    )
    env = environment(case)
    backend = FixtureBackend(env.world, env.evaluator)
    recorder = EventRecorder(
        run_id=plan.run_id,
        task_id=plan.task_id,
        candidate_id=plan.candidate_id,
        skill_versions=plan.skill_versions,
        model_versions=plan.model_versions,
        clock=env.clock,
    )
    if selector is None and plan.trial is None:
        selector = RULE_SELECTORS[protocol][1]()
    elif selector is None:
        selector = TrialSelector()
    driver = _Driver(plan, recorder, selector, backend, env.clock, budgets, provider)
    bundle, report = driver.execute()
    trial_verdict: CandidateVerdict | None = None
    if plan.trial is not None:
        trial_verdict = env.evaluator.candidate_verdict(
            plan.trial.skill_id,
            plan.trial.skill_version,
            plan.trial.completion["expected_progress"],
        )
    return CaseRun(
        case_id=case.case_id,
        bundle=bundle,
        report=report,
        verdict=copy.deepcopy(env.evaluator.verdict),
        audit=env.world.audit(),
        candidate=trial_verdict,
    )


def run_fixture_case(
    case: FixtureCase,
    snapshot: RegistrySnapshot,
    *,
    candidate: tuple[str, int] | None = None,
    bodies: Mapping[str, SkillBody] | None = None,
    selector: DecisionSource | None = None,
) -> CaseRun:
    """Execute one frozen case against a fresh fixture world and evaluator.

    ``selector`` replaces the rule (or trial) selector, for tests of decision validation only.
    """
    return run_case(
        case,
        snapshot,
        fixture_environment,
        candidate=candidate,
        bodies=bodies,
        selector=selector,
    )


def replay_case(
    bundle: Any,
    snapshot: RegistrySnapshot,
    *,
    candidate: tuple[str, int] | None = None,
    bodies: Mapping[str, SkillBody] | None = None,
    planner_id: str | None = None,
    budgets: Budgets = BASELINE_BUDGETS,
    protocol: str = TASK_PROTOCOL,
) -> ReplayReport:
    """Recorded-decision replay of a case bundle through the same harness logic.

    No world, evaluator or selector runs: decisions and tool/monitor results come from the bundle.
    Raises ``EvidenceError`` for invalid evidence or changed skill/selector versions and
    ``ReplayDivergence`` when the logic asks for something that was not recorded.
    """
    validate_bundle(bundle)
    plan = plan_case(
        bundle["manifest"]["task_id"],
        snapshot,
        candidate=candidate,
        bodies=bodies,
        planner_id=planner_id,
        protocol=protocol,
    )

    def runner(
        recorder: EventRecorder, decisions: DecisionSource, tools: ToolExecutor
    ) -> dict[str, Any]:
        driver = _Driver(plan, recorder, decisions, tools, _Counter(), budgets)
        replayed, _report = driver.execute()
        return replayed

    return replay(
        bundle,
        ReplayMode.RECORDED_DECISION,
        runner,
        skill_versions=plan.skill_versions,
        model_versions=plan.model_versions,
    )


def correctness_failures(
    case: FixtureCase, run: CaseRun, *, model_calls: int | None = 0
) -> list[str]:
    """Names (from ``CORRECTNESS_CHECKS``) of the frozen checks this run fails; empty if none.

    ``model_calls`` is the expected model-call count; ``None`` skips that check (model planner).
    """
    try:
        validate_bundle(run.bundle)
        semantic = semantic_outcome(run.bundle)
    except ValueError:
        return ["evidence_valid"]
    verdict = run.verdict
    if verdict is None:
        return ["evidence_valid", "primary", "progress", "admitted", "violations"]
    expected = case.expected
    observed = {
        "primary": (verdict["primary"], expected["primary"]),
        "progress": (verdict["progress"], expected["progress"]),
        "state": (run.report.state, expected["state"]),
        "status": (run.report.status, expected["status"]),
        "attempts": (run.report.usage["attempts"]["used"], expected["attempts"]),
        "admitted": (
            {tool: verdict["admitted"][tool] for tool in expected["admitted"]},
            dict(expected["admitted"]),
        ),
        "violations": (verdict["violations"], 0),
        "model_calls": (
            run.report.usage["model_calls"]["used"],
            run.report.usage["model_calls"]["used"] if model_calls is None else model_calls,
        ),
        "bundle_outcome": (semantic.outcome, (verdict["primary"], verdict["progress"])),
        "bundle_state": (semantic.status, run.report.state),
    }
    return [name for name, (actual, wanted) in observed.items() if actual != wanted]


def export_evidence(runs: Iterable[CaseRun], directory: str | Path) -> dict[str, str]:
    """Write each bundle to ``<directory>/<case_id>.json`` (never overwriting) and return the
    SHA-256 of every written file, keyed by case ID."""
    target = Path(directory)
    digests: dict[str, str] = {}
    for run in runs:
        path = target / f"{run.case_id}.json"
        write_bundle(run.bundle, path)
        digests[run.case_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests
