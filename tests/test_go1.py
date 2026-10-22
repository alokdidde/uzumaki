"""Unitree Go1 in MuJoCo (``uzumaki.sim.go1``): a quadruped standing on its legs, gripping with
a jaw on its neck.

Like ``tests/test_sim.py``, these need the simulator requirements and the pinned Go1 and UMI
gripper assets (``python -m uzumaki.sim.assets fetch go1 umi``); without them they skip, and with
``UZUMAKI_REQUIRE_SIM=1`` (``make sim``) a missing simulator fails instead of skipping.
"""

import copy
import json
import math
import os
from pathlib import Path

import pytest

from uzumaki.baseline import correctness_failures, replay_case
from uzumaki.fixture_env import load_cases
from uzumaki.sim import SimUnavailable
from uzumaki.sim.assets import GO1_MODEL, UMI_MODEL

REPO = Path(__file__).resolve().parents[1]
REQUIRE_ENV = "UZUMAKI_REQUIRE_SIM"
CASES = load_cases(REPO / "src/uzumaki/sim/data/go1_cases_v1.json")
CASE_IDS = [case.case_id for case in CASES]


def _simulator_problem() -> str | None:
    try:
        import mujoco  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return "simulator requirements are not installed (requirements-sim.txt)"
    try:
        GO1_MODEL.verify()
        UMI_MODEL.verify()
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


def test_go1_and_jaw_assets_are_pinned_with_their_licenses() -> None:
    assert len(GO1_MODEL.files) == 7 and len(UMI_MODEL.files) == 10
    assert "LICENSE" in GO1_MODEL.files and "go1.xml" in GO1_MODEL.files
    assert "LICENSE" in UMI_MODEL.files and "umi_gripper.xml" in UMI_MODEL.files
    assert (GO1_MODEL.env_var, UMI_MODEL.env_var) == ("UZUMAKI_GO1_ASSETS", "UZUMAKI_UMI_ASSETS")


def test_case_set_is_the_frozen_geometry_unchanged(sim: None) -> None:
    """Eight frozen baseline fault cases as they are, then one nominal case per protocol
    development geometry; the file is exactly what ``build_case_set`` makes of the two sources."""
    from uzumaki.sim.go1 import CASES_FILE, build_case_set

    built = build_case_set(
        REPO / "tests/fixtures/baseline_cases_v1.json", REPO / "docs/protocol/task-v1.json"
    )
    assert json.loads(CASES_FILE.read_text(encoding="utf-8")) == built
    frozen = load_cases(REPO / "tests/fixtures/baseline_cases_v1.json")
    assert CASES[:8] == frozen
    assert [case.geometry for case in CASES[8:]] == [f"dev-{i:03d}" for i in range(1, 21)]
    assert all(case.expected["primary"] == "completed" for case in CASES[8:])


def test_inverse_kinematics_covers_the_tabletop(sim: None) -> None:
    import mujoco
    import numpy as np

    from uzumaki.sim import arm, go1
    from uzumaki.sim.scene import build_model

    kin = arm.Kinematics(build_model(CASES[0], GO1_MODEL.verify(), go1.RIG), go1.RIG)
    for x, y in ((-0.15, 0.15), (0.15, -0.15), (0.0, 0.2), (-0.15, -0.15)):
        q = kin.solve((x, y, 0.03), math.radians(30))
        assert q is not None, (x, y)
        kin.data.qpos[kin.qpos] = q
        mujoco.mj_kinematics(kin.model, kin.data)
        assert np.linalg.norm(kin.data.site_xpos[kin.site] - np.array((x, y, 0.03))) < 1e-3
        assert kin.data.site_xmat[kin.site].reshape(3, 3)[2, 0] < -0.999  # jaw points down
    assert kin.solve((0.0, 0.8, 0.03), 0.0) is None  # beyond reach


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_meets_frozen_expectations_and_replays(sim: None, case_id: str) -> None:
    """The SO-101's driver, rule selector and skills, unchanged, on a dog's jaw."""
    from uzumaki.sim import go1
    from uzumaki.sim.run import load_snapshot, run_sim_case

    registry = load_snapshot(REPO / "tests/fixtures/skills_pick_place_v1.json")
    case = next(case for case in CASES if case.case_id == case_id)
    run, world = run_sim_case(case, registry, rig=go1.RIG)
    assert correctness_failures(case, run) == []
    assert run.audit["contacts"] == []
    report = replay_case(copy.deepcopy(run.bundle), registry)
    assert report.matches, report.differences
    if case.expected["primary"] == "completed":
        final = world.final_state()
        assert final["xy_error_m"] <= 0.02 and final["yaw_error_deg"] <= 10


def test_go1_is_a_conformant_quadruped_with_jaw(sim: None) -> None:
    from uzumaki import conformance
    from uzumaki.embodiments import embodiment
    from uzumaki.tasks import PICK_PLACE

    results = conformance.check(embodiment("go1"), PICK_PLACE, "go1-dev-005")
    assert all(r.status == "pass" for r in results), [(r.name, r.detail) for r in results]
    assert len(results) == 8


def test_go1_stands_on_its_feet_while_the_jaw_works(sim: None) -> None:
    """The body is free: it stays up on the leg actuators, only the lower legs touch the floor,
    and the planner follows the stance the physics holds."""
    import numpy as np

    from uzumaki.fixture_env import FixtureClock
    from uzumaki.sim import go1
    from uzumaki.sim.world import new_world

    world = new_world(CASES[0], FixtureClock(), GO1_MODEL.verify(), rig=go1.RIG)
    assert world.ready
    assert world.model.jnt_type[0] == 0  # the trunk's floating base
    assert world.model.body(world.model.joint("neck_yaw").bodyid[0]).parentid[0] == (
        world.model.body("trunk").id
    )
    height = float(world.data.qpos[2]) - go1.FLOOR_Z_M
    seen = world.observe({})
    estimate = seen["object_pose_estimate"]
    reply = world.pick(
        {
            "observation_id": seen["observation_id"],
            **{key: estimate[key] for key in ("x_m", "y_m", "yaw_deg")},
            "grasp_height_m": CASES[0].box_size_m / 2,
        }
    )
    assert reply["status"] == "ok" and reply["held"]
    assert abs(float(world.data.qpos[2]) - go1.FLOOR_Z_M - height) < 0.01
    assert world.contact_events == []
    assert np.allclose(world.kin.data.qpos[:7], world.data.qpos[:7], atol=0.02)
