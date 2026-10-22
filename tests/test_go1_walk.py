"""The walking Unitree Go1 in MuJoCo (``uzumaki.sim.go1_walk``): a learned walking policy on its
legs and a jaw on its neck, on ``fetch-v1``.

Like ``tests/test_sim.py``, these need the simulator requirements (with ``onnxruntime``) and the
pinned assets (``python -m uzumaki.sim.assets fetch go1 umi go1_walk``); without them they skip,
and with ``UZUMAKI_REQUIRE_SIM=1`` (``make sim``) a missing simulator fails instead of skipping.
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
from uzumaki.sim.assets import GO1_MODEL, GO1_WALK_MODEL, UMI_MODEL

REPO = Path(__file__).resolve().parents[1]
REQUIRE_ENV = "UZUMAKI_REQUIRE_SIM"
CASES = load_cases(REPO / "src/uzumaki/sim/data/go1_walk_cases_v1.json", FETCH_PROTOCOL)
CASE_IDS = [case.case_id for case in CASES]


def _simulator_problem() -> str | None:
    try:
        import mujoco  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return "simulator requirements are not installed (requirements-sim.txt)"
    try:
        for model in (GO1_MODEL, UMI_MODEL, GO1_WALK_MODEL):
            model.verify()
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


def test_policy_and_model_are_pinned_from_playground() -> None:
    from uzumaki.sim.assets import GO1_WALK_POLICY, GO1_WALK_XML, PLAYGROUND_COMMIT

    assert set(GO1_WALK_MODEL.files) == {"LICENSE", GO1_WALK_XML, GO1_WALK_POLICY}
    assert GO1_WALK_MODEL.commit == PLAYGROUND_COMMIT
    assert GO1_WALK_MODEL.source_url == (
        f"https://raw.githubusercontent.com/google-deepmind/mujoco_playground/{PLAYGROUND_COMMIT}/"
    )


def test_case_set_is_the_seed_recipe_output(sim: None) -> None:
    from uzumaki.sim.go1_walk import CASES_FILE, build_case_set

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


def test_the_dog_walks_to_the_station_and_stands(sim: None) -> None:
    from uzumaki.sim.go1_walk import (
        ARRIVED_M,
        HOME_HEIGHT_M,
        RIG,
        TABLE_HEIGHT_M,
        new_go1_walk_world,
    )

    world = new_go1_walk_world(CASES[0], FixtureClock(), RIG.assets.verify())
    assert world.ready
    seen = world.observe({})
    assert seen["within_reach"] == {"object": False, "target": False}
    assert seen["base_pose"]["x_m"] == pytest.approx(0.4, abs=0.01)
    target = seen["target_pose"]
    arrived = world.go_to(
        {"observation_id": seen["observation_id"], "x_m": target["x_m"], "y_m": target["y_m"]}
    )
    assert arrived["status"] == "ok"
    # It walked about 0.4 m sideways on its feet: still upright, at standing height.
    assert math.isclose(arrived["base_pose"]["x_m"], target["x_m"], abs_tol=2 * ARRIVED_M)
    assert abs(arrived["base_pose"]["yaw_deg"] - 90.0) < 6.0
    trunk = world.data.body("trunk")
    assert trunk.xmat[8] > 0.99
    assert trunk.xpos[2] + TABLE_HEIGHT_M > 0.9 * HOME_HEIGHT_M
    assert world.command is None  # the legs hold the stance it stopped in
    assert world.observe({})["within_reach"] == {"object": False, "target": True}
    assert world.contact_events == []


def test_the_policy_stands_still_on_a_zero_command(sim: None) -> None:
    import numpy as np

    from uzumaki.sim.go1_walk import RIG, new_go1_walk_world

    world = new_go1_walk_world(CASES[0], FixtureClock(), RIG.assets.verify())
    start = world.base()
    world.command = np.zeros(3, dtype=np.float32)
    world._hold(1.0)
    settled = world.base()
    # It shifts its posture under the neck's weight once, then keeps its feet where they are.
    assert np.linalg.norm(settled[:2] - start[:2]) < 0.04
    world._hold(2.0)
    assert np.linalg.norm(world.base()[:2] - settled[:2]) < 0.01
    assert abs(world.base()[2] - settled[2]) < math.radians(2.0)
    assert world.contact_events == []


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_meets_frozen_expectations_and_replays(sim: None, case_id: str) -> None:
    """The baseline driver and skills with ``fetch-selector-v1``, on a robot that walks."""
    from uzumaki.sim.go1_walk import RIG
    from uzumaki.sim.run import FETCH_SKILLS, load_snapshot, run_sim_case

    registry = load_snapshot(REPO / FETCH_SKILLS, {tool: "1.0" for tool in FETCH_TOOLS})
    case = next(case for case in CASES if case.case_id == case_id)
    run, world = run_sim_case(case, registry, protocol=FETCH_PROTOCOL, rig=RIG)
    assert correctness_failures(case, run) == []
    assert run.audit["contacts"] == []
    report = replay_case(copy.deepcopy(run.bundle), registry, protocol=FETCH_PROTOCOL)
    assert report.matches, report.differences
    if case.expected["primary"] == "completed":
        final = world.final_state()
        assert final["xy_error_m"] <= 0.02 and final["yaw_error_deg"] <= 10


def test_walking_go1_is_a_conformant_quadruped_with_jaw(sim: None) -> None:
    from uzumaki import conformance
    from uzumaki.embodiments import embodiment
    from uzumaki.tasks import FETCH

    results = conformance.check(embodiment("go1_walk"), FETCH, "go1-walk-dev-009")
    assert all(r.status == "pass" for r in results), [(r.name, r.detail) for r in results]
    binding = next(r for r in results if r.name == "task_binding")
    assert "go_to: legs_1+legs_2+legs_3+legs_4" in binding.detail
