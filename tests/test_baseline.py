"""Offline task-execution baseline (issue 08): frozen fixture cases, replay and candidate trials.

Every frozen case in ``tests/fixtures/baseline_cases_v1.json`` runs goal -> observation ->
eligibility -> rule-based selection -> bounded skill execution -> retries -> independent
verification -> event bundle against the scripted fixture world, with no model provider. Each run
must pass every frozen correctness check and replay (recorded decisions and results) to the same
semantic outcome. Development trials show that motionless and report-only candidates are rejected
while a candidate that really moves the box is accepted.
"""

import copy
import dataclasses
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from uzumaki import providers
from uzumaki.baseline import (
    BASELINE_ID,
    CORRECTNESS_CHECKS,
    SETTLE_OBSERVES,
    TRIAL_SELECTOR_ID,
    CaseRun,
    RuleSelector,
    SkillContext,
    SkillResult,
    correctness_failures,
    export_evidence,
    pick_and_place,
    pick_box,
    plan_case,
    replay_case,
    run_fixture_case,
)
from uzumaki.evidence import EvidenceError, load_bundle
from uzumaki.fixture_env import (
    FixtureCase,
    FixtureClock,
    FixtureEvaluator,
    FixtureWorld,
    load_cases,
)
from uzumaki.registry import RegistrySnapshot, SkillRegistry, load_skill_set, seal_skill
from uzumaki.replay import ReplayDivergence, ReplayMode, semantic_outcome

FIXTURES = Path(__file__).parent / "fixtures"
CASES_FILE = FIXTURES / "baseline_cases_v1.json"
SKILLS_FILE = FIXTURES / "skills_pick_place_v1.json"
CANDIDATES_FILE = FIXTURES / "candidates_pick_place_v1.json"
#: Directory for the retained raw bundles and hash manifest (``make baseline-evidence``).
EVIDENCE_ENV = "UZUMAKI_BASELINE_EVIDENCE_DIR"
#: The fixtures issue 08 requires; ``stale-after-pick`` is an additional frozen case.
REQUIRED_CASES = (
    "nominal",
    "unreachable-goal",
    "failed-grasp",
    "stale-observation",
    "retry-exhausted",
    "cancellation",
    "worker-crash",
)
CASES = load_cases(CASES_FILE)
CASE_IDS = [case.case_id for case in CASES]
POSITIVE_CONTROL = "pick-place-candidate"
#: Observes after release until completion holds: 200 ms settle delay plus the 500 ms window at
#: 100 ms per fixture observe.
SETTLE = ["observe"] * 7


def case_named(case_id: str) -> FixtureCase:
    return next(case for case in CASES if case.case_id == case_id)


def skill_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["skills"]
    return entries


def positive_control() -> dict[str, Any]:
    """A candidate copy of ``pick-and-place@1``: it really picks and places the box."""
    for entry in skill_entries(SKILLS_FILE):
        if entry["artifact"]["skill_id"] == "pick-and-place":
            artifact = copy.deepcopy(entry["artifact"])
            artifact["skill_id"] = POSITIVE_CONTROL
            artifact["instructions"] = "Positive control: pick, observe again, then place."
            return {"state": "candidate", "evidence_ref": None, "artifact": artifact}
    raise KeyError("pick-and-place")


def build_registry(directory: Path) -> SkillRegistry:
    """Seal the hand-authored skills, adversarial candidates and positive control; load them."""
    entries = skill_entries(SKILLS_FILE) + skill_entries(CANDIDATES_FILE) + [positive_control()]
    document = {
        "format": "uzumaki.skillset/1.0",
        "skills": [dict(entry, artifact=seal_skill(entry["artifact"])) for entry in entries],
    }
    path = directory / "skills.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_skill_set(path)


@pytest.fixture(autouse=True)
def no_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("model provider constructed in the offline baseline")

    monkeypatch.setattr(providers.RecordedProvider, "__init__", refuse)


@pytest.fixture
def registry(tmp_path: Path) -> SkillRegistry:
    return build_registry(tmp_path)


@pytest.fixture
def snapshot(registry: SkillRegistry) -> RegistrySnapshot:
    return registry.snapshot()


# --- hand-authored trial bodies and test selectors ---


def hold_still(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``hold-still@1``: observe, never actuate, honestly report no release."""
    ctx.observe()
    return SkillResult({"released": False})


def report_success(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """``report-success@1``: claim the box was released without calling any actuating tool."""
    return SkillResult({"released": True})


def place_without_permission(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """A ``pick-box@1`` body that asks for ``place``, which pick-box@1 does not permit."""
    target = ctx.observation["target_pose"]
    ctx.call("place", {"observation_id": ctx.observation["observation_id"], **target})
    return SkillResult({"held": True})


def half_height_pick(ctx: SkillContext, parameters: Mapping[str, Any]) -> SkillResult:
    """A changed ``pick-box@1`` body: grasps at half the selected height."""
    return pick_box(ctx, {"grasp_height_m": parameters["grasp_height_m"] / 2})


TRIAL_BODIES = {
    "hold-still@1": hold_still,
    "report-success@1": report_success,
    f"{POSITIVE_CONTROL}@1": pick_and_place,
}


class FixedSelector:
    """Returns one fixed decision and keeps the requests it saw."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.requests: list[dict[str, Any]] = []

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        return copy.deepcopy(self.output)


class ReportFirstSelector:
    """Selects the trial candidate for the first decision, then defers to the rule selector."""

    def __init__(self) -> None:
        self.rule = RuleSelector()

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        trial = request["trial_candidate"]
        if decision_id == "d1" and trial is not None:
            return {
                "action": "select",
                "skill_id": trial["skill_id"],
                "skill_version": trial["skill_version"],
                "parameters": {},
            }
        return self.rule.decide(decision_id, role, request)


def kinds(bundle: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event["data"] for event in bundle["events"] if event["kind"] == kind]


def world_tools(bundle: Mapping[str, Any]) -> list[str]:
    """World tool calls in order (monitor calls to the evaluator excluded)."""
    return [
        event["data"]["tool"]
        for event in bundle["events"]
        if event["kind"] == "tool_call" and event["data"]["tool"] != "monitor"
    ]


def retain(runs: list[CaseRun], directory: Path) -> None:
    """Write the raw bundles and a hash manifest to an explicitly requested directory."""
    directory.mkdir(parents=True, exist_ok=True)
    digests = export_evidence(runs, directory)
    manifest = {
        "format": "uzumaki.baseline-evidence/1.0",
        "cases_sha256": hashlib.sha256(CASES_FILE.read_bytes()).hexdigest(),
        "skills_sha256": hashlib.sha256(SKILLS_FILE.read_bytes()).hexdigest(),
        "candidates_sha256": hashlib.sha256(CANDIDATES_FILE.read_bytes()).hexdigest(),
        "selector": BASELINE_ID,
        "bundles": {
            run.case_id: {
                "sha256": digests[run.case_id],
                "head_sha256": run.bundle["manifest"]["head_sha256"],
                "outcome": kinds(run.bundle, "outcome")[0],
                "state": run.report.state,
                "status": run.report.status,
                "correctness_failures": correctness_failures(case_named(run.case_id), run),
            }
            for run in runs
        },
    }
    with (directory / "manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


# --- frozen fixture cases ---


def test_frozen_case_file_covers_every_required_fixture() -> None:
    assert set(REQUIRED_CASES) <= set(CASE_IDS)
    assert len(set(CASE_IDS)) == len(CASE_IDS) == 8


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_frozen_case_is_correct(case_id: str, snapshot: RegistrySnapshot) -> None:
    case = case_named(case_id)
    run = run_fixture_case(case, snapshot)
    assert correctness_failures(case, run) == []
    expected = case.expected
    outcome = kinds(run.bundle, "outcome")
    assert outcome == [{"primary": expected["primary"], "progress": expected["progress"]}]
    assert kinds(run.bundle, "run_finished") == [{"status": expected["state"]}]
    assert run.report.status == expected["status"]
    assert run.report.usage["model_calls"]["used"] == 0
    assert run.audit["violations"] == 0
    assert run.bundle["manifest"]["model_versions"] == {"planner": BASELINE_ID}
    assert run.bundle["manifest"]["candidate_id"] is None
    assert run.candidate is None
    assert {decision["model"] for decision in kinds(run.bundle, "decision")} <= {BASELINE_ID}


FAILURE_PATHS = [
    ("unreachable-goal", ["observe", "pick", "observe", "place"], "blocked"),
    ("failed-grasp", ["observe", "pick", "observe", "pick", "observe", "place", *SETTLE], None),
    ("stale-observation", ["observe", "observe", "pick", "observe", "place", *SETTLE], None),
    ("stale-after-pick", ["observe", "pick", "observe", "observe", "place", *SETTLE], None),
    ("retry-exhausted", ["observe", "pick"] * 3, "attempts"),
    ("cancellation", ["observe", "pick", "abort"], "safe_stop_reported"),
    ("worker-crash", ["observe", "pick", "observe"], "worker_failure"),
]


@pytest.mark.parametrize(("case_id", "tools", "detail"), FAILURE_PATHS)
def test_failure_path_stops_where_expected(
    case_id: str,
    tools: list[str],
    detail: str | None,
    snapshot: RegistrySnapshot,
) -> None:
    run = run_fixture_case(case_named(case_id), snapshot)
    assert world_tools(run.bundle) == tools
    reached = tools[:-1] if case_id == "worker-crash" else tools  # the crash precedes the world
    assert run.audit["requests"] == reached
    if detail is not None:
        assert run.report.detail == detail


def test_nominal_bundle_records_the_whole_loop(snapshot: RegistrySnapshot) -> None:
    case = case_named("nominal")
    run = run_fixture_case(case, snapshot)
    bundle = run.bundle
    assert len(SETTLE) <= SETTLE_OBSERVES
    assert world_tools(bundle) == ["observe", "pick", "observe", "place", *SETTLE]
    assert run.report.usage["steps"]["used"] == 11
    assert bundle["events"][0]["kind"] == "run_started"
    assert [event["kind"] for event in bundle["events"][-2:]] == ["outcome", "run_finished"]
    observations = kinds(bundle, "observation")
    assert len(observations) == 11
    assert {observation["source"] for observation in observations} == {"fixture_world"}
    calls = kinds(bundle, "tool_call")
    skills = {call["tool"]: call["skill_id"] for call in calls}
    assert skills == {"observe": None, "pick": "pick-box", "place": "place-box", "monitor": None}
    events = [call["arguments"]["event"] for call in calls if call["tool"] == "monitor"]
    assert events == ["skill_report", "skill_report", *["status"] * 7, "final"]
    decisions = kinds(bundle, "decision")
    assert [decision["model"] for decision in decisions] == [BASELINE_ID, BASELINE_ID]
    assert [decision["output"]["skill_id"] for decision in decisions] == ["pick-box", "place-box"]
    height = round(case.box_size_m / 2, 6)
    assert decisions[0]["output"]["parameters"] == {"grasp_height_m": height}
    assert decisions[1]["output"]["parameters"] == {"approach": "vertical"}
    assert kinds(bundle, "tool_result")[-1]["result"] == run.verdict
    assert (run.report.progress, run.report.progress_source) == ("goal_reached", "evaluator")
    assert set(bundle["manifest"]["skill_versions"]) == {
        "observe-scene",
        "pick-and-place",
        "pick-box",
        "place-box",
    }


def test_correctness_checks_detect_a_wrong_expectation(snapshot: RegistrySnapshot) -> None:
    case = case_named("nominal")
    run = run_fixture_case(case, snapshot)
    expected = {**case.expected, "primary": "abstained", "attempts": 2}
    wrong = dataclasses.replace(case, expected=expected)
    failures = correctness_failures(wrong, run)
    assert failures == ["primary", "attempts"]
    assert set(failures) <= set(CORRECTNESS_CHECKS)


def test_world_rejects_actuation_on_a_superseded_observation() -> None:
    world = FixtureWorld(case_named("nominal"), FixtureClock())
    first = world.observe({})
    world.observe({})
    args = {
        "observation_id": first["observation_id"],
        **first["object_pose_estimate"],
        "grasp_height_m": 0.02,
    }
    reply = world.pick(args)
    assert (reply["status"], reply["reason"]) == ("rejected", "stale_observation")
    assert world.held is False
    assert world.picks == 0
    audit = world.audit()
    assert audit["admitted"]["pick"] == 0
    assert audit["rejected"]["pick"] == 1
    assert audit["violations"] == 0


# --- semantic replay against fresh execution ---


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_semantic_replay_matches_fresh_execution(
    case_id: str,
    snapshot: RegistrySnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_fixture_case(case_named(case_id), snapshot)
    again = run_fixture_case(case_named(case_id), snapshot)
    assert again.bundle == run.bundle  # fresh fixture execution is deterministic
    assert again.verdict == run.verdict

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay ran the world, the evaluator or the selector")

    monkeypatch.setattr(FixtureWorld, "__init__", refuse)
    monkeypatch.setattr(FixtureEvaluator, "__init__", refuse)
    monkeypatch.setattr(RuleSelector, "decide", refuse)
    report = replay_case(run.bundle, snapshot)
    assert report.mode is ReplayMode.RECORDED_DECISION
    assert report.matches, report.differences
    assert report.differences == ()
    assert semantic_outcome(report.replayed) == semantic_outcome(run.bundle)
    head = run.bundle["manifest"]["head_sha256"]
    assert report.replayed["manifest"]["origin"] == {
        "mode": "recorded_decision",
        "source_head_sha256": head,
    }


def test_replay_detects_divergent_skill_logic(snapshot: RegistrySnapshot) -> None:
    run = run_fixture_case(case_named("nominal"), snapshot)
    with pytest.raises(ReplayDivergence):
        replay_case(run.bundle, snapshot, bodies={"pick-box@1": half_height_pick})


def test_replay_refuses_changed_skill_versions(registry: SkillRegistry) -> None:
    run = run_fixture_case(case_named("nominal"), registry.snapshot())
    registry.deprecate("pick-box", 1, evidence_ref="test.deprecated")
    with pytest.raises(EvidenceError) as caught:
        replay_case(run.bundle, registry.snapshot())
    assert caught.value.reason == "version_mismatch"


def test_replay_refuses_tampered_evidence(snapshot: RegistrySnapshot) -> None:
    run = run_fixture_case(case_named("nominal"), snapshot)
    bundle = copy.deepcopy(run.bundle)
    assert bundle["events"][1]["kind"] == "tool_call"
    bundle["events"][1]["data"]["arguments"] = {"edited": True}
    with pytest.raises(EvidenceError) as caught:
        replay_case(bundle, snapshot)
    assert caught.value.reason == "corrupt"
    tampered = dataclasses.replace(run, bundle=bundle)
    assert correctness_failures(case_named("nominal"), tampered) == ["evidence_valid"]


# --- selector decisions, eligibility and tool permissions ---


INVALID_DECISIONS = [
    pytest.param({"action": "execute", "code": "pass"}, id="unsupported-action"),
    pytest.param(
        {"action": "select", "skill_id": "push-box", "skill_version": 1, "parameters": {}},
        id="candidate-not-eligible",
    ),
    pytest.param(
        {"action": "select", "skill_id": "observe-scene", "skill_version": 1, "parameters": {}},
        id="eligible-without-body",
    ),
    pytest.param(
        {
            "action": "select",
            "skill_id": "pick-box",
            "skill_version": 2,
            "parameters": {"grasp_height_m": 0.02},
        },
        id="unknown-version",
    ),
    pytest.param(
        {
            "action": "select",
            "skill_id": "pick-box",
            "skill_version": "1",
            "parameters": {"grasp_height_m": 0.02},
        },
        id="version-not-integer",
    ),
    pytest.param(
        {
            "action": "select",
            "skill_id": "pick-box",
            "skill_version": 1,
            "parameters": {"grasp_height_m": 0.5},
        },
        id="parameters-out-of-bounds",
    ),
]


@pytest.mark.parametrize("output", INVALID_DECISIONS)
def test_invalid_selector_decision_fails_the_run_before_actuation(
    output: dict[str, Any],
    snapshot: RegistrySnapshot,
) -> None:
    selector = FixedSelector(output)
    run = run_fixture_case(case_named("nominal"), snapshot, selector=selector)
    assert (run.report.state, run.report.detail) == ("failed", "invalid_decision")
    assert world_tools(run.bundle) == ["observe"]
    assert run.audit["requests"] == ["observe"]
    assert run.verdict is not None
    assert (run.verdict["primary"], run.verdict["progress"]) == ("evaluation_failure", "none")
    assert [decision["output"] for decision in kinds(run.bundle, "decision")] == [output]
    eligible = [entry["skill_id"] for entry in selector.requests[0]["eligible"]]
    assert eligible == ["observe-scene", "pick-and-place", "pick-box", "place-box"]
    assert selector.requests[0]["trial_candidate"] is None


def test_abstaining_selector_stops_without_actuation(snapshot: RegistrySnapshot) -> None:
    selector = FixedSelector({"action": "abstain", "reason": "no_eligible_skill"})
    run = run_fixture_case(case_named("nominal"), snapshot, selector=selector)
    assert (run.report.state, run.report.status) == ("completed", "abstained")
    assert run.verdict is not None
    assert run.verdict["primary"] == "abstained"
    assert world_tools(run.bundle) == ["observe"]


def test_selector_sees_only_currently_eligible_skills(registry: SkillRegistry) -> None:
    registry.deprecate("place-box", 1, evidence_ref="test.deprecated")
    run = run_fixture_case(case_named("nominal"), registry.snapshot())
    assert (run.report.state, run.report.status) == ("completed", "partial_progress")
    assert run.verdict is not None
    assert (run.verdict["primary"], run.verdict["progress"]) == ("partial_progress", "partial")
    assert world_tools(run.bundle) == ["observe", "pick", "observe"]
    decisions = [decision["output"] for decision in kinds(run.bundle, "decision")]
    assert decisions[1] == {"action": "abstain", "reason": "no_eligible_skill"}
    assert set(run.bundle["manifest"]["skill_versions"]) == {"observe-scene", "pick-box"}


def test_tool_outside_skill_permissions_is_never_sent(snapshot: RegistrySnapshot) -> None:
    bodies = {"pick-box@1": place_without_permission}
    run = run_fixture_case(case_named("nominal"), snapshot, bodies=bodies)
    assert run.audit["requests"] == ["observe", "observe", "observe"]
    assert run.audit["admitted"] == {"pick": 0, "place": 0, "abort": 0}
    assert run.audit["rejected"] == {"pick": 0, "place": 0, "abort": 0}
    assert (run.report.state, run.report.status) == ("completed", "budget_exhausted")
    assert run.verdict is not None
    assert run.verdict["primary"] == "budget_exhausted"
    monitor = [call["arguments"] for call in kinds(run.bundle, "tool_call")]
    failures = [args["failure"] for args in monitor if args.get("event") == "attempt_end"]
    assert failures == ["tool_rejected"] * 3
    reports = [args["report"] for args in monitor if args.get("event") == "skill_report"]
    assert reports == [{}] * 3
    replayed = replay_case(run.bundle, snapshot, bodies=bodies)
    assert replayed.matches, replayed.differences


# --- development trials: motionless and report-only candidates ---


@pytest.mark.parametrize(
    ("skill_id", "reason"),
    [
        ("hold-still", "no_actual_progress"),
        ("report-success", "success_report_without_progress"),
    ],
)
def test_motionless_and_report_only_candidates_are_rejected(
    skill_id: str,
    reason: str,
    snapshot: RegistrySnapshot,
) -> None:
    run = run_fixture_case(
        case_named("nominal"),
        snapshot,
        candidate=(skill_id, 1),
        bodies=TRIAL_BODIES,
    )
    verdict = run.candidate
    assert verdict is not None
    assert verdict.accepted is False
    assert verdict.reason == reason
    assert verdict.invocations == 3
    assert verdict.effects == 0
    assert verdict.unsupported_claims == (3 if skill_id == "report-success" else 0)
    assert verdict.primary == "budget_exhausted"
    assert run.report.status == "budget_exhausted"
    assert run.verdict is not None
    assert run.verdict["progress"] == "none"
    assert run.audit["admitted"] == {"pick": 0, "place": 0, "abort": 0}
    assert run.bundle["manifest"]["candidate_id"] == f"{skill_id}:1"
    assert run.bundle["manifest"]["model_versions"] == {"planner": TRIAL_SELECTOR_ID}
    assert snapshot.state(skill_id, 1) == "candidate"  # trials never change registry state
    replayed = replay_case(run.bundle, snapshot, candidate=(skill_id, 1), bodies=TRIAL_BODIES)
    assert replayed.matches, replayed.differences


def test_report_only_candidate_is_not_accepted_when_the_case_completes_another_way(
    snapshot: RegistrySnapshot,
) -> None:
    run = run_fixture_case(
        case_named("nominal"),
        snapshot,
        candidate=("report-success", 1),
        bodies=TRIAL_BODIES,
        selector=ReportFirstSelector(),
    )
    assert run.verdict is not None
    assert run.verdict["primary"] == "completed"
    assert run.report.usage["attempts"]["used"] == 2
    decisions = [decision["output"]["skill_id"] for decision in kinds(run.bundle, "decision")]
    assert decisions == ["report-success", "pick-box", "place-box"]
    verdict = run.candidate
    assert verdict is not None
    assert (verdict.accepted, verdict.reason) == (False, "success_report_without_progress")
    assert (verdict.invocations, verdict.effects, verdict.unsupported_claims) == (1, 0, 1)
    assert verdict.primary == "completed"


def test_candidate_that_moves_the_box_is_accepted(snapshot: RegistrySnapshot) -> None:
    run = run_fixture_case(
        case_named("nominal"),
        snapshot,
        candidate=(POSITIVE_CONTROL, 1),
        bodies=TRIAL_BODIES,
    )
    verdict = run.candidate
    assert verdict is not None
    assert (verdict.accepted, verdict.reason, verdict.primary) == (True, "accepted", "completed")
    assert (verdict.invocations, verdict.effects, verdict.unsupported_claims) == (1, 1, 0)
    assert run.audit["admitted"] == {"pick": 1, "place": 1, "abort": 0}
    assert run.audit["violations"] == 0
    assert world_tools(run.bundle) == ["observe", "pick", "observe", "place", *SETTLE]
    calls = kinds(run.bundle, "tool_call")
    actuations = [call for call in calls if call["tool"] in ("pick", "place")]
    assert {call["skill_id"] for call in actuations} == {POSITIVE_CONTROL}
    assert snapshot.state(POSITIVE_CONTROL, 1) == "candidate"
    replayed = replay_case(
        run.bundle,
        snapshot,
        candidate=(POSITIVE_CONTROL, 1),
        bodies=TRIAL_BODIES,
    )
    assert replayed.matches, replayed.differences


def test_trial_candidate_needs_a_hand_authored_body(snapshot: RegistrySnapshot) -> None:
    with pytest.raises(ValueError, match="no implementation"):
        plan_case("nominal", snapshot, candidate=("push-box", 1))


# --- raw evidence ---


def test_bundles_export_with_hashes_and_never_overwrite(
    snapshot: RegistrySnapshot,
    tmp_path: Path,
) -> None:
    runs = [run_fixture_case(case, snapshot) for case in CASES]
    target = tmp_path / "evidence"
    target.mkdir()
    digests = export_evidence(runs, target)
    assert sorted(digests) == sorted(CASE_IDS)
    for run in runs:
        path = target / f"{run.case_id}.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digests[run.case_id]
        assert load_bundle(path) == run.bundle
    with pytest.raises(EvidenceError):
        export_evidence(runs[:1], target)
    requested = os.environ.get(EVIDENCE_ENV)
    if requested:
        retain(runs, Path(requested))
