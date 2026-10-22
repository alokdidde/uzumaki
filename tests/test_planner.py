"""Model planner (``uzumaki.planner``) in the offline fixture loop with a fake model.

The planner's answers are proposals: the run counts and bounds every model call, invalid answers
are retried within budget, and the driver validates each choice again before any actuation.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from model_fakes import RATE_LIMITED, CompetentModel, context_of
from uzumaki.baseline import (
    BASELINE_SKILL_BODIES,
    MODEL_BUDGETS,
    CaseRun,
    correctness_failures,
    fixture_environment,
    replay_case,
    run_case,
)
from uzumaki.fixture_env import load_cases
from uzumaki.planner import PROMPT_VERSION, SYSTEM_PROMPT, ModelPlanner, parse_decision
from uzumaki.registry import RegistrySnapshot, load_skill_set, seal_skill

FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "baseline_cases_v1.json")
MODEL = "anthropic/claude-sonnet-5.5"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> RegistrySnapshot:
    document = json.loads((FIXTURES / "skills_pick_place_v1.json").read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path_factory.mktemp("skills") / "skills.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_skill_set(path).snapshot()


def case_named(case_id: str) -> Any:
    return next(case for case in CASES if case.case_id == case_id)


def run_with(snapshot: RegistrySnapshot, case_id: str, model: Any) -> CaseRun:
    planner = ModelPlanner(snapshot, MODEL, frozenset(BASELINE_SKILL_BODIES))
    return run_case(
        case_named(case_id),
        snapshot,
        fixture_environment,
        selector=planner,
        planner_id=planner.model_version,
        budgets=MODEL_BUDGETS,
        provider=model,
    )


def decisions(run: CaseRun) -> list[dict[str, Any]]:
    return [event["data"] for event in run.bundle["events"] if event["kind"] == "decision"]


@pytest.mark.parametrize("case_id", [case.case_id for case in CASES])
def test_model_planner_meets_every_frozen_expectation(
    snapshot: RegistrySnapshot, case_id: str
) -> None:
    model = CompetentModel()
    run = run_with(snapshot, case_id, model)
    assert correctness_failures(case_named(case_id), run, model_calls=None) == []
    made = decisions(run)
    assert run.report.usage["model_calls"]["used"] == len(made) == len(model.requests)
    for decision in made:
        assert decision["model"] == f"{MODEL}+{PROMPT_VERSION}"
        assert decision["output"]["model"]["usage"] == {"input_tokens": 900, "output_tokens": 40}
    report = replay_case(
        copy.deepcopy(run.bundle),
        snapshot,
        planner_id=f"{MODEL}+{PROMPT_VERSION}",
        budgets=MODEL_BUDGETS,
    )
    assert report.matches, report.differences


def test_context_is_bounded_data_without_evaluator_state(snapshot: RegistrySnapshot) -> None:
    model = CompetentModel()
    run_with(snapshot, "nominal", model)
    request = model.requests[0]
    assert request["system"] == SYSTEM_PROMPT and request["prompt_version"] == PROMPT_VERSION
    context = context_of(request)
    assert set(context) == {"task", "observation", "eligible_skills"}
    assert set(context["observation"]) == {"observation_id", "gripper_state", "target_pose"}
    offered = {card["skill_id"] for card in context["eligible_skills"]}
    assert offered == {"pick-box", "place-box", "pick-and-place"}  # observe-scene has no body
    text = json.dumps(request)
    for private in ("initial_xy_m", "box_size_m", "heldout", '"expected"', "misgrasp"):
        assert private not in text


@pytest.mark.parametrize(
    "bad",
    [
        "I will pick the box now.",
        {"action": "select", "skill_id": "teleport-box", "skill_version": 1, "parameters": {},
         "reason": "invented"},
        {"action": "select", "skill_id": "pick-box", "skill_version": 9,
         "parameters": {"grasp_height_m": 0.02}, "reason": "wrong version"},
        {"action": "run_code", "reason": "import os"},
        {"action": "abstain", "reason": "x", "success": True},
    ],
)  # fmt: skip
def test_invalid_answers_are_retried_within_budget(snapshot: RegistrySnapshot, bad: Any) -> None:
    run = run_with(snapshot, "nominal", CompetentModel([bad]))
    assert run.verdict is not None and run.verdict["primary"] == "completed"
    assert run.report.usage["model_calls"]["used"] == len(decisions(run)) + 1


def test_persistently_invalid_answers_fail_before_actuation(snapshot: RegistrySnapshot) -> None:
    run = run_with(snapshot, "nominal", CompetentModel(["no json"] * 3))
    assert run.report.status == "budget_exhausted"
    assert run.audit["admitted"]["pick"] == 0 and run.audit["admitted"]["place"] == 0
    assert run.report.usage["model_calls"]["used"] == 3  # first call plus two retries


def test_rate_limits_are_retried(snapshot: RegistrySnapshot) -> None:
    run = run_with(snapshot, "nominal", CompetentModel([RATE_LIMITED, RATE_LIMITED]))
    assert run.verdict is not None and run.verdict["primary"] == "completed"


def test_parameters_outside_the_contract_never_actuate(snapshot: RegistrySnapshot) -> None:
    wild = {"action": "select", "skill_id": "pick-box", "skill_version": 1,
            "parameters": {"grasp_height_m": 5.0}, "reason": "high grasp"}  # fmt: skip
    run = run_with(snapshot, "nominal", CompetentModel([wild]))
    assert run.report.status == "failed" and run.report.detail == "invalid_decision"
    assert run.audit["admitted"]["pick"] == 0


def test_abstention_stops_without_actuation(snapshot: RegistrySnapshot) -> None:
    run = run_with(snapshot, "nominal", CompetentModel([{"action": "abstain", "reason": "unsure"}]))
    assert run.report.status == "abstained"
    assert run.verdict is not None and run.verdict["primary"] == "abstained"
    assert run.audit["admitted"]["pick"] == 0


def test_a_success_claim_is_not_completion(snapshot: RegistrySnapshot) -> None:
    """The planner can only pick skills; the evaluator alone decides completion."""
    claim = {"action": "abstain", "reason": "the box is already on the target, task complete"}
    run = run_with(snapshot, "nominal", CompetentModel([claim]))
    assert run.verdict is not None and run.verdict["primary"] == "abstained"
    assert run.verdict["progress"] == "none"


def test_parse_decision_strips_code_fences_and_bounds_reasons() -> None:
    offered = {("pick-box", 1)}
    fenced = '```json\n{"action": "abstain", "reason": "' + "x" * 1000 + '"}\n```'
    decision = parse_decision(fenced, offered)
    assert decision == {"action": "abstain", "reason": "x" * 300}
    assert parse_decision('{"action": "abstain"}', offered) is None
    assert parse_decision("[1]", offered) is None
