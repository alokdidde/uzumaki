"""ALOHA in MuJoCo (``uzumaki.sim.aloha``): two arms hand a bar over on ``handover-v1``.

Like ``tests/test_sim.py``, these need the simulator requirements and the pinned ALOHA assets
(``python -m uzumaki.sim.assets fetch aloha``); without them they skip, and with
``UZUMAKI_REQUIRE_SIM=1`` (``make sim``) a missing simulator fails instead of skipping.
"""

import copy
import json
import os
from pathlib import Path

import pytest

from uzumaki.baseline import correctness_failures, replay_case
from uzumaki.fixture_env import HANDOVER_PROTOCOL, HANDOVER_TOOLS, load_cases
from uzumaki.sim import SimUnavailable
from uzumaki.sim.assets import ALOHA_MODEL

REPO = Path(__file__).resolve().parents[1]
REQUIRE_ENV = "UZUMAKI_REQUIRE_SIM"
CASES = load_cases(REPO / "src/uzumaki/sim/data/aloha_cases_v1.json", HANDOVER_PROTOCOL)
CASE_IDS = [case.case_id for case in CASES]


def _simulator_problem() -> str | None:
    try:
        import mujoco  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return "simulator requirements are not installed (requirements-sim.txt)"
    try:
        ALOHA_MODEL.verify()
    except SimUnavailable as error:
        return str(error)
    return None


PROBLEM = _simulator_problem()


@pytest.fixture
def sim() -> None:
    if PROBLEM is None:
        return
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"simulation requested but unavailable: {PROBLEM}")
    pytest.skip(PROBLEM)


def test_aloha_assets_are_pinned_with_their_license() -> None:
    assert len(ALOHA_MODEL.files) == 16
    assert {"LICENSE", "aloha.xml", "joint_position_actuators.xml"} <= set(ALOHA_MODEL.files)


def test_case_set_is_the_seed_recipe_output(sim: None) -> None:
    from uzumaki.sim.aloha import CASES_FILE, build_case_set

    assert json.loads(CASES_FILE.read_text(encoding="utf-8")) == build_case_set()
    assert [case.case_id for case in CASES[:8]] == [
        "nominal",
        "unreachable-goal",
        "failed-grasp",
        "dropped-handover",
        "stale-observation",
        "retry-exhausted",
        "cancellation",
        "worker-crash",
    ]
    assert all(case.initial_xy_m[0] < 0 < case.target_xy_m[0] for case in CASES)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_meets_frozen_expectations_and_replays(sim: None, case_id: str) -> None:
    """The baseline driver and skills with ``handover-selector-v1``, on two physical arms."""
    from uzumaki.sim.run import HANDOVER_SKILLS, load_snapshot, run_sim_case

    registry = load_snapshot(REPO / HANDOVER_SKILLS, {tool: "1.0" for tool in HANDOVER_TOOLS})
    case = next(case for case in CASES if case.case_id == case_id)
    run, world = run_sim_case(case, registry, protocol=HANDOVER_PROTOCOL)
    assert correctness_failures(case, run) == []
    assert run.audit["contacts"] == []
    report = replay_case(copy.deepcopy(run.bundle), registry, protocol=HANDOVER_PROTOCOL)
    assert report.matches, report.differences
    if case.expected["primary"] == "completed":
        final = world.final_state()
        assert final["xy_error_m"] <= 0.02 and final["yaw_error_deg"] <= 10


def test_aloha_is_a_conformant_pair_of_arms(sim: None) -> None:
    from uzumaki import conformance
    from uzumaki.embodiments import embodiment
    from uzumaki.tasks import HANDOVER

    results = conformance.check(embodiment("aloha"), HANDOVER, "aloha-dev-009")
    assert all(r.status == "pass" for r in results), [(r.name, r.detail) for r in results]
    binding = next(r for r in results if r.name == "task_binding")
    assert "handover: left+right" in binding.detail
