"""Stanford TidyBot in MuJoCo (``uzumaki.sim.tidybot``): a mobile manipulator on ``fetch-v1``.

Like ``tests/test_sim.py``, these need the simulator requirements and the pinned TidyBot assets
(``python -m uzumaki.sim.assets fetch tidybot``); without them they skip, and with
``UZUMAKI_REQUIRE_SIM=1`` (``make sim``) a missing simulator fails instead of skipping.
"""

import copy
import json
import math
import os
from pathlib import Path

import pytest

from uzumaki.baseline import correctness_failures, replay_case
from uzumaki.fetch_env import table_under
from uzumaki.fixture_env import FETCH_PROTOCOL, FETCH_TOOLS, FixtureClock, load_cases
from uzumaki.sim import SimUnavailable
from uzumaki.sim.assets import TIDYBOT_MODEL

REPO = Path(__file__).resolve().parents[1]
REQUIRE_ENV = "UZUMAKI_REQUIRE_SIM"
CASES = load_cases(REPO / "src/uzumaki/sim/data/tidybot_cases_v1.json", FETCH_PROTOCOL)
CASE_IDS = [case.case_id for case in CASES]


def _simulator_problem() -> str | None:
    try:
        import mujoco  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return "simulator requirements are not installed (requirements-sim.txt)"
    try:
        TIDYBOT_MODEL.verify()
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


def test_tidybot_assets_are_pinned_with_every_license() -> None:
    assert len(TIDYBOT_MODEL.files) == 22
    assert {"LICENSE", "tidybot.xml"} <= set(TIDYBOT_MODEL.files)
    assert set(TIDYBOT_MODEL.notices) == {"kinova_gen3/LICENSE", "robotiq_2f85/LICENSE"}


def test_case_set_is_the_seed_recipe_output(sim: None) -> None:
    from uzumaki.sim.tidybot import CASES_FILE, build_case_set

    assert json.loads(CASES_FILE.read_text(encoding="utf-8")) == build_case_set()
    assert [case.case_id for case in CASES[:8]] == [
        "nominal",
        "unreachable-goal",
        "failed-grasp",
        "stopped-short",
        "stale-observation",
        "retry-exhausted",
        "cancellation",
        "worker-crash",
    ]
    assert all(table_under(*case.initial_xy_m) == "a" for case in CASES)
    assert all(
        table_under(*case.target_xy_m) == "b"
        for case in CASES
        if case.case_id != "unreachable-goal"
    )


def test_reach_is_judged_from_where_the_base_stands(sim: None) -> None:
    from uzumaki.sim.tidybot import RIG, new_tidybot_world

    world = new_tidybot_world(CASES[0], FixtureClock(), RIG.assets.verify())
    assert world.ready
    seen = world.observe({})
    assert seen["within_reach"] == {"object": False, "target": False}
    assert seen["base_pose"] == {"x_m": 0.4, "y_m": -0.52, "yaw_deg": 90.0}
    target = seen["target_pose"]
    arrived = world.go_to(
        {"observation_id": seen["observation_id"], "x_m": target["x_m"], "y_m": target["y_m"]}
    )
    assert arrived["status"] == "ok"
    assert math.isclose(arrived["base_pose"]["x_m"], target["x_m"], abs_tol=1e-3)
    assert world.observe({})["within_reach"] == {"object": False, "target": True}
    assert world.contact_events == []


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_meets_frozen_expectations_and_replays(sim: None, case_id: str) -> None:
    """The baseline driver and skills with ``fetch-selector-v1``, on a robot that drives."""
    from uzumaki.sim.run import FETCH_SKILLS, load_snapshot, run_sim_case

    registry = load_snapshot(REPO / FETCH_SKILLS, {tool: "1.0" for tool in FETCH_TOOLS})
    case = next(case for case in CASES if case.case_id == case_id)
    run, world = run_sim_case(case, registry, protocol=FETCH_PROTOCOL)
    assert correctness_failures(case, run) == []
    assert run.audit["contacts"] == []
    report = replay_case(copy.deepcopy(run.bundle), registry, protocol=FETCH_PROTOCOL)
    assert report.matches, report.differences
    if case.expected["primary"] == "completed":
        final = world.final_state()
        assert final["xy_error_m"] <= 0.02 and final["yaw_error_deg"] <= 10


def test_tidybot_is_a_conformant_mobile_manipulator(sim: None) -> None:
    from uzumaki import conformance
    from uzumaki.embodiments import embodiment
    from uzumaki.tasks import FETCH

    results = conformance.check(embodiment("tidybot"), FETCH, "tidybot-dev-009")
    assert all(r.status == "pass" for r in results), [(r.name, r.detail) for r in results]
    binding = next(r for r in results if r.name == "task_binding")
    assert "go_to: base" in binding.detail
