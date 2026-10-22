"""Generator role (features 12/13): answer parsing, static checks, lineage and session budgets.

These tests never start a restricted worker: every program here is rejected before evaluation, or
the session stops first. Evaluation of generated programs in the worker is in
``test_generator_worker.py`` (admitted platforms) and ``test_sim.py`` (MuJoCo).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from generated_programs import CLAIMS_ONLY, ESCAPE, TRANSFER, ScriptedGenerator, fenced
from model_fakes import RATE_LIMITED
from uzumaki.baseline import fixture_environment
from uzumaki.broker import ToolBroker
from uzumaki.budgets import Budgets
from uzumaki.fixture_env import load_cases
from uzumaki.generator import (
    MAX_PROGRAM_BYTES,
    PROMPT_VERSION,
    ROLE,
    TRANSFER_BOX,
    build_context,
    check_program,
    extract_program,
    generate_skill,
    syntax_digest,
)
from uzumaki.registry import SkillRegistry, load_skill_set, seal_skill
from uzumaki.sandbox import RestrictedWorker

FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "baseline_cases_v1.json")
TOOLS = TRANSFER_BOX.tools


def registry(tmp_path: Path) -> SkillRegistry:
    document = json.loads((FIXTURES / "skills_pick_place_v1.json").read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_skill_set(path)


def no_worker(broker: ToolBroker) -> RestrictedWorker:
    raise AssertionError("a program reached the restricted worker")


def session(tmp_path: Path, programs: list[Any], **kwargs: Any) -> Any:
    model = ScriptedGenerator(programs)
    reg = registry(tmp_path)
    report = generate_skill(
        TRANSFER_BOX,
        reg,
        CASES,
        fixture_environment,
        provider=model,
        model="test-model",
        worker=no_worker,
        **kwargs,
    )
    return report, model, reg


# --------------------------------------------------------------------------- parsing
def test_answer_must_hold_exactly_one_python_block() -> None:
    assert extract_program(fenced(TRANSFER)) == TRANSFER.strip("\n") + "\n"
    assert extract_program("```\ndef run(tools, payload):\n    pass\n```") is not None
    assert extract_program(TRANSFER) is None  # no fence
    assert extract_program(fenced(TRANSFER) + fenced(TRANSFER)) is None
    assert extract_program("```python\n   \n```") is None
    assert extract_program(None) is None


def test_well_formed_programs_pass_static_checks() -> None:
    with_math = "import math\n\ndef run(tools, payload):\n    return {}\n"
    for program in (TRANSFER, CLAIMS_ONLY, with_math):
        assert check_program(program, TOOLS) == []


@pytest.mark.parametrize(
    ("program", "reasons"),
    [
        (ESCAPE, ["forbidden_import", "forbidden_name"]),
        ("def run(tools, payload)\n    pass\n", ["syntax_error"]),
        ("def main(tools, payload):\n    return {}\n", ["missing_run"]),
        ("def run(tools, payload=None):\n    return {}\n", ["missing_run"]),
        ("def run(tools, payload):\n    return tools.call(payload['t'], {})\n",
         ["dynamic_tool_name"]),
        ("def run(tools, payload):\n    return tools.call('gripper', {})\n", ["unknown_tool"]),
        ("def run(tools, payload):\n    return payload.__class__\n",
         ["private_attribute"]),
        ("def run(tools, payload):\n    return tools._channel\n", ["private_attribute"]),
        ("def run(tools, payload):\n    return eval('1')\n", ["forbidden_name"]),
        ("def run(tools, payload):\n    return __builtins__\n", ["forbidden_name"]),
        ("from os import path\n\ndef run(tools, payload):\n    return {}\n",
         ["forbidden_import"]),
        ("def run(tools, payload):\n    import socket\n    return {}\n", ["forbidden_import"]),
        ("print('loaded')\n\ndef run(tools, payload):\n    return {}\n", ["top_level_code"]),
        ("X = tools.call('pick', {})\n\ndef run(tools, payload):\n    return {}\n",
         ["top_level_code"]),
        ("class A:\n    pass\n\ndef run(tools, payload):\n    return {}\n",
         ["forbidden_construct", "top_level_code"]),
        ("def run(tools, payload):\n    with x:\n        pass\n", ["forbidden_construct"]),
    ],
)  # fmt: skip
def test_static_checks_reject_with_fixed_reasons(program: str, reasons: list[str]) -> None:
    assert check_program(program, TOOLS) == reasons


def test_oversized_programs_are_rejected_before_parsing() -> None:
    program = "def run(tools, payload):\n    return {}\n" + "#" * MAX_PROGRAM_BYTES
    assert check_program(program, TOOLS) == ["oversized"]
    assert check_program(b"def run(tools, payload): pass", TOOLS) == ["not_text"]


def test_syntax_digest_ignores_layout_and_comments() -> None:
    a = "def run(tools, payload):\n    return {}\n"
    b = "# draft\ndef run(tools,   payload):\n\n    return {}  # same\n"
    assert syntax_digest(a) == syntax_digest(b)
    assert syntax_digest("def run(:") is None


# ------------------------------------------------------------------------- sessions
def test_first_request_carries_the_capability_and_no_case_data(tmp_path: Path) -> None:
    report, model, _ = session(tmp_path, [ESCAPE], max_rounds=1)
    request = model.requests[0]
    assert request["role"] == ROLE and request["prompt_version"] == PROMPT_VERSION
    assert request["model"] == "test-model"
    content = request["messages"][0]["content"]
    for case in CASES:
        assert case.case_id not in content
    context = build_context(TRANSFER_BOX, None)
    assert json.dumps(context, indent=1) in content
    assert context["capability"]["tools"] == ["abort", "observe", "pick", "place"]
    assert report.status == "rounds_exhausted"


def test_rejected_programs_never_reach_the_worker_or_the_registry(tmp_path: Path) -> None:
    second = ESCAPE.replace("import os\n", "import subprocess\n")
    report, model, reg = session(tmp_path, [ESCAPE, second], max_rounds=2)
    assert report.status == "rounds_exhausted"
    assert [p.static_rejections for p in report.programs] == [
        ["forbidden_import", "forbidden_name"],
        ["forbidden_import", "forbidden_name"],
    ]
    assert all(p.skill is None and not p.results for p in report.programs)
    assert not any(sid == "transfer-box" for sid, _ in reg.snapshot().keys())
    repair = json.loads(model.requests[1]["messages"][0]["content"].split("```json\n")[1][:-4])
    assert repair["repair"] == {
        "previous_program": ESCAPE.strip("\n") + "\n",
        "static_check": {"rejected": ["forbidden_import", "forbidden_name"]},
    }
    assert report.programs[1].parent == 1


def test_repeated_identical_repair_stops_without_another_round(tmp_path: Path) -> None:
    reformatted = "# again\n" + ESCAPE
    report, model, _ = session(tmp_path, [ESCAPE, reformatted, TRANSFER], max_rounds=4)
    assert report.status == "repeated_candidate"
    assert len(model.requests) == 2
    assert len(report.programs) == 2
    assert report.session.status == "blocked"


def test_invalid_answers_are_retried_within_the_session_budget(tmp_path: Path) -> None:
    report, model, _ = session(
        tmp_path,
        [{"text": "I cannot see a fenced block here."}, RATE_LIMITED, ESCAPE],
        max_rounds=1,
    )
    assert len(model.requests) == 3
    assert report.status == "rounds_exhausted"
    assert report.session.usage["model_calls"]["used"] == 3


def test_budget_exhaustion_ends_the_session(tmp_path: Path) -> None:
    budgets = Budgets(
        wall_ms=60_000,
        model_calls=2,
        steps=0,
        retries=1,
        max_output_bytes=32_768,
        max_total_output_bytes=65_536,
        attempts=1,
    )
    invalid = {"text": "no program"}
    report, model, _ = session(tmp_path, [invalid, invalid, invalid], budgets=budgets)
    assert report.status == "budget_exhausted"
    assert report.programs == [] and report.accepted is None
    assert len(model.requests) == 2


def test_session_needs_development_cases_and_sane_rounds(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    model = ScriptedGenerator([])
    for rounds, cases in ((0, CASES), (17, CASES), (2, ())):
        with pytest.raises(ValueError):
            generate_skill(
                TRANSFER_BOX,
                reg,
                cases,
                fixture_environment,
                provider=model,
                model="m",
                max_rounds=rounds,
                worker=no_worker,
            )
    assert model.requests == []


def test_artifact_binds_the_program_digest_and_keeps_the_host_contract() -> None:
    artifact = TRANSFER_BOX.artifact(3, "ab" * 32)
    assert artifact["skill_id"] == "transfer-box" and artifact["skill_version"] == 3
    assert "ab" * 32 in artifact["instructions"]
    assert artifact["completion"]["success_authority"] == "environment_evaluator"
    assert [t["name"] for t in artifact["tool_permissions"]] == [
        "observe",
        "pick",
        "place",
        "abort",
    ]
