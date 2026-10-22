"""Generated programs in the restricted worker against the fixture world (features 13/16).

Needs an admitted platform (see docs/features/restricted-worker.md); elsewhere these tests skip,
and ``UZUMAKI_REQUIRE_CONTAINMENT=1`` (``make containment``) turns the skip into a failure.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from generated_programs import (
    CLAIMS_ONLY,
    NO_RESUME,
    STALE_PLACE,
    TRANSFER,
    ScriptedGenerator,
)
from uzumaki.baseline import fixture_environment, replay_case
from uzumaki.fixture_env import load_cases
from uzumaki.generator import TRANSFER_BOX, evaluate_program, generate_skill
from uzumaki.registry import SkillRegistry, load_skill_set, seal_skill
from uzumaki.sandbox import ContainmentUnavailable, admit, probe_platform

FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "baseline_cases_v1.json")


def _admission() -> str | None:
    try:
        admit(probe_platform())
    except ContainmentUnavailable as error:
        return str(error)
    return None


UNSUPPORTED = _admission()
if UNSUPPORTED is not None and os.environ.get("UZUMAKI_REQUIRE_CONTAINMENT") == "1":
    raise RuntimeError(f"containment required but platform not admitted: {UNSUPPORTED}")

pytestmark = pytest.mark.skipif(
    UNSUPPORTED is not None, reason=f"unverified platform: {UNSUPPORTED}"
)


def registry(tmp_path: Path) -> SkillRegistry:
    document = json.loads((FIXTURES / "skills_pick_place_v1.json").read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_skill_set(path)


def context(request: dict[str, Any]) -> dict[str, Any]:
    content = request["messages"][0]["content"]
    data: dict[str, Any] = json.loads(content.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    return data


def register(reg: SkillRegistry, source: str, version: int = 1) -> tuple[str, int]:
    from uzumaki.generator import program_sha256

    skill = reg.register(TRANSFER_BOX.artifact(version, program_sha256(source)))
    return skill.key


def test_two_repairs_from_trace_feedback_reach_acceptance(tmp_path: Path) -> None:
    model = ScriptedGenerator([STALE_PLACE, NO_RESUME, TRANSFER])
    reg = registry(tmp_path)
    report = generate_skill(
        TRANSFER_BOX, reg, CASES, fixture_environment, provider=model, model="test-model"
    )
    assert report.status == "accepted"
    assert [p.skill for p in report.programs] == [
        "transfer-box@1",
        "transfer-box@2",
        "transfer-box@3",
    ]
    assert [p.parent for p in report.programs] == [None, 1, 2]
    assert [sum(r.passed for r in p.results) for p in report.programs] == [3, 7, 8]
    assert report.accepted is report.programs[2]
    snapshot = reg.snapshot()
    assert snapshot.state("transfer-box", 1) == "rejected"
    assert snapshot.state("transfer-box", 2) == "rejected"
    assert snapshot.state("transfer-box", 3) == "candidate"  # promotion is feature 17
    assert report.session.status == "completed"
    assert report.session.usage["model_calls"]["used"] == 3

    # Round 2 sees the root cause of round 1: placing with the observation it picked with.
    first = context(model.requests[1])["repair"]
    assert first["previous_program"] == STALE_PLACE.strip("\n") + "\n"
    failed = {case["case_id"]: case for case in first["development_results"]["failed_cases"]}
    nominal = failed["nominal"]
    assert nominal["failed_stage"] == "perception"
    assert nominal["expected"] == {"primary": "completed", "status": "completed"}
    place = next(call for call in nominal["tool_calls"] if call["tool"] == "place")
    assert place["status"] == "rejected" and place["result"] == {"reason": "stale_observation"}
    assert failed["failed-grasp"]["failed_stage"] == "execution"  # the misgrasp comes first

    # Round 3 sees why resuming failed: picking again while already holding the box.
    second = context(model.requests[2])["repair"]["development_results"]
    assert [case["case_id"] for case in second["failed_cases"]] == ["stale-after-pick"]
    resume = second["failed_cases"][0]
    assert resume["failed_stage"] == "planning"
    assert {"reason": "already_held"} in [call.get("result") for call in resume["tool_calls"]]

    # Feedback never carries evaluator internals or the world's privileged state.
    text = json.dumps([context(r) for r in model.requests])
    for secret in ("admitted", "invariant_evidence", "box_size_m", "misgrasp_picks"):
        assert secret not in text


def test_accepted_program_replays_from_its_bundles(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    key = register(reg, TRANSFER)
    results = evaluate_program(reg.snapshot(), key, TRANSFER, CASES, fixture_environment)
    assert all(result.passed for result in results)
    from uzumaki.generator import SandboxedProgram

    label = f"{key[0]}@{key[1]}"
    for result in results:
        replayed = replay_case(
            result.run.bundle,
            reg.snapshot(),
            candidate=key,
            bodies={label: SandboxedProgram(TRANSFER)},
        )
        assert replayed.matches, (result.case_id, replayed.differences)


def test_success_claims_without_action_are_not_accepted(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    key = register(reg, CLAIMS_ONLY)
    (result,) = evaluate_program(reg.snapshot(), key, CLAIMS_ONLY, CASES[:1], fixture_environment)
    assert not result.passed
    assert result.verdict == "success_report_without_progress"
    assert result.observed["primary"] != "completed"


@pytest.mark.parametrize(
    ("body", "detail", "status", "error"),
    [
        ("return 1 / 0", "candidate_program_failed", "candidate_error", "ZeroDivisionError"),
        ("return {'outputs': {}}", "invalid_candidate_output", "returned", None),
        ("return {'outputs': {}, 'failure': 'completed'}", "invalid_candidate_output",
         "returned", None),
        ("return {'outputs': {'held': True}, 'failure': None}", "invalid_skill_output",
         "returned", None),
        ("while True:\n        tools.call('observe', {})", "candidate_program_failed",
         "tool_budget_exhausted", None),
    ],
)  # fmt: skip
def test_broken_programs_fail_the_run_with_program_feedback(
    tmp_path: Path, body: str, detail: str, status: str, error: str | None
) -> None:
    source = f"def run(tools, payload):\n    {body}\n"
    reg = registry(tmp_path)
    key = register(reg, source)
    (result,) = evaluate_program(reg.snapshot(), key, source, CASES[:1], fixture_environment)
    assert not result.passed
    assert result.observed["status"] == "failed"
    assert result.observed["detail"] == detail
    assert result.program_runs[0]["status"] == status
    assert result.program_runs[0]["error"] == error
    assert result.failed_stage in ("program", "output_contract")


def test_harness_stops_inside_a_program_call_are_raised_in_the_host(tmp_path: Path) -> None:
    """The worker-crash case crashes the world on its third call, inside the program's observe;
    the run fails exactly as it does for hand-authored skills."""
    reg = registry(tmp_path)
    key = register(reg, TRANSFER)
    crash = next(case for case in CASES if case.case_id == "worker-crash")
    (result,) = evaluate_program(reg.snapshot(), key, TRANSFER, [crash], fixture_environment)
    assert result.passed
    assert result.observed["primary"] == "evaluation_failure"
    assert result.observed["status"] == "failed"
    calls = result.calls
    assert [call["tool"] for call in calls] == ["pick", "observe"]
    assert calls[-1]["result"] == {"reason": "worker_crashed"}
