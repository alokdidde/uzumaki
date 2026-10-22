"""MuJoCo SO-101 simulation of ``pick-place-v1`` (``uzumaki.sim``).

The case-set and asset-pinning checks need no simulator and always run. Everything else needs the
pinned simulator requirements (``requirements-sim.txt``) and verified assets
(``python -m uzumaki.sim.assets fetch``); without them those tests skip, and with
``UZUMAKI_REQUIRE_SIM=1`` (``make sim``) a missing simulator fails instead of skipping.
"""

import copy
import dataclasses
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from uzumaki.baseline import correctness_failures, replay_case
from uzumaki.fixture_env import FixtureCase, FixtureClock, load_cases
from uzumaki.sim import SimUnavailable, assets
from uzumaki.sim.cases import BASE_XY, CASES_FILE, RANGES, generate_geometry, load_so101_cases

FIXTURES = Path(__file__).parent / "fixtures"
SKILLS_FILE = FIXTURES / "skills_pick_place_v1.json"
REQUIRE_ENV = "UZUMAKI_REQUIRE_SIM"
CASES = load_so101_cases()
CASE_IDS = [case.case_id for case in CASES]


def _simulator_problem() -> str | None:
    try:
        import mujoco  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return "simulator requirements are not installed (requirements-sim.txt)"
    try:
        assets.verify_assets()
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


def case_named(case_id: str) -> FixtureCase:
    return next(case for case in CASES if case.case_id == case_id)


def snapshot() -> Any:
    from uzumaki.sim.run import load_snapshot

    return load_snapshot(SKILLS_FILE)


# ------------------------------------------------------------------ no simulator needed
def test_case_set_is_the_seed_recipe_output() -> None:
    """Every frozen geometry is regenerated from its seed; only the unreachable goal is moved."""
    fixture = {case.case_id: case for case in load_cases(FIXTURES / "baseline_cases_v1.json")}
    assert len(CASES) == 28
    for index, case in enumerate(CASES):
        geometry = generate_geometry(f"so101-{1001 + index}")
        assert case.box_size_m == geometry["box_size_m"]
        assert list(case.initial_xy_m) == geometry["initial_xy_m"]
        assert case.initial_yaw_deg == geometry["initial_yaw_deg"]
        if case.case_id == "unreachable-goal":
            assert case.target_xy_m == (0.4, 0.0)
        else:
            assert list(case.target_xy_m) == geometry["target_xy_m"]
        if case.case_id in fixture:  # fault cases mirror the fixture baseline exactly
            mirrored = fixture[case.case_id]
            assert case.expected == mirrored.expected
            assert case.misgrasp_picks == mirrored.misgrasp_picks
            assert case.stale_observes == mirrored.stale_observes
            assert case.crash_on_call == mirrored.crash_on_call
            assert case.withdraw_goal_after_call == mirrored.withdraw_goal_after_call


def test_case_geometry_stays_in_the_declared_ranges() -> None:
    for case in CASES:
        low, high = RANGES["size"]
        assert low <= case.box_size_m <= high
        for (x, y), side in ((case.initial_xy_m, -1), (case.target_xy_m, 1)):
            if (x, y) == (0.4, 0.0):
                continue
            radius = math.hypot(x - BASE_XY[0], y - BASE_XY[1])
            bearing = math.degrees(math.atan2(side * (x - BASE_XY[0]), y - BASE_XY[1]))
            assert 0.15 - 1e-6 <= radius <= 0.23 + 1e-6
            assert 25 - 1e-4 <= bearing <= 60 + 1e-4


def test_assets_refuse_a_tampered_file(tmp_path: Path) -> None:
    with pytest.raises(SimUnavailable, match="missing"):
        assets.verify_assets(tmp_path)
    if PROBLEM is not None:
        return
    copy_dir = tmp_path / "copy"
    shutil.copytree(assets.verify_assets(), copy_dir)
    assert assets.verify_assets(copy_dir) == copy_dir
    target = copy_dir / "so101.xml"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(SimUnavailable, match="pinned hash: so101.xml"):
        assets.verify_assets(copy_dir)


def test_asset_manifest_is_stable() -> None:
    assert len(assets.FILES) == 21
    assert assets.manifest_sha256() == assets.manifest_sha256()
    assert CASES_FILE.is_file()


# ---------------------------------------------------------------------- simulator
@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_meets_frozen_expectations_and_replays(sim: None, case_id: str) -> None:
    """Same driver, rule selector and skills as the fixture baseline, physics underneath."""
    from uzumaki.sim.run import run_sim_case

    registry = snapshot()
    case = case_named(case_id)
    run, world = run_sim_case(case, registry)
    assert correctness_failures(case, run) == []
    assert run.audit["contacts"] == []
    report = replay_case(copy.deepcopy(run.bundle), registry)
    assert report.matches, report.differences
    if case.expected["primary"] == "completed":
        final = world.final_state()
        assert final["xy_error_m"] <= 0.02 and final["yaw_error_deg"] <= 10


def test_runs_are_deterministic(sim: None) -> None:
    from uzumaki.sim.run import run_sim_case

    registry = snapshot()
    first, _ = run_sim_case(case_named("nominal"), registry)
    second, _ = run_sim_case(case_named("nominal"), registry)
    assert json.dumps(first.bundle, sort_keys=True) == json.dumps(second.bundle, sort_keys=True)


def test_observations_carry_only_declared_fields(sim: None) -> None:
    from uzumaki.sim.world import new_world

    world = new_world(case_named("nominal"), FixtureClock(), assets.verify_assets())
    observation = world.observe({})
    assert set(observation) == {
        "status",
        "observation_id",
        "timestamp_ms",
        "object_pose_estimate",
        "target_pose",
        "gripper_state",
        "tool_result",
        "at_ms",
    }
    assert observation["at_ms"] - observation["timestamp_ms"] == 0
    assert world.clock() == 1100  # one observe holds the arm for 100 ms of physics


def test_actuation_is_refused_before_observe_without_motion(sim: None) -> None:
    from uzumaki.sim.world import new_world

    world = new_world(case_named("nominal"), FixtureClock(), assets.verify_assets())
    qpos = world.data.qpos.copy()
    result = world.pick({"observation_id": "obs-1", "x_m": -0.1, "y_m": 0.0, "yaw_deg": 0.0,
                         "grasp_height_m": 0.02})  # fmt: skip
    assert result == {"status": "rejected", "reason": "before_observe", "at_ms": 1000}
    assert (world.data.qpos == qpos).all() and world.clock() == 1000
    assert world.audit()["violations"] == 0


def test_unreachable_pick_is_rejected_without_motion(sim: None) -> None:
    from uzumaki.sim.world import new_world

    world = new_world(case_named("nominal"), FixtureClock(), assets.verify_assets())
    observation = world.observe({})
    started = world.clock()
    result = world.pick({"observation_id": observation["observation_id"], "x_m": 0.0,
                         "y_m": -0.19, "yaw_deg": 0.0, "grasp_height_m": 0.02})  # fmt: skip
    assert result["status"] == "rejected" and result["reason"] == "unreachable_target"
    assert world.clock() == started and world.picks == 0


def test_forbidden_contact_ends_the_case_as_collision(sim: None) -> None:
    """Driving the arm into the tabletop is recorded by the physics monitor, not by the skill."""
    import numpy as np

    from uzumaki.sim.world import SimEvaluator, new_world

    world = new_world(case_named("nominal"), FixtureClock(), assets.verify_assets())
    evaluator = SimEvaluator(world)
    assert evaluator.handle({"event": "status"}).result["terminal"] is None
    world._move(np.array([0.0, 1.6, 0.0, 0.0, 0.0]), 1.5)  # shoulder lift down into the table
    assert world.contact_events, "no forbidden contact recorded"
    assert evaluator.handle({"event": "status"}).result["terminal"] == "collision"
    verdict = evaluator.handle({"event": "final", "status": "completed"}).result
    assert verdict["primary"] == "collision" and verdict["violations"] >= 1


def test_failed_reset_is_an_evaluation_failure(sim: None) -> None:
    """A box spawned overlapping the arm's shoulder does not come to rest cleanly."""
    from uzumaki.sim.world import SimEvaluator, new_world

    case = dataclasses.replace(
        case_named("nominal"), box_size_m=0.09, initial_xy_m=(BASE_XY[0], BASE_XY[1] + 0.04)
    )
    world = new_world(case, FixtureClock(), assets.verify_assets())
    assert not world.ready
    status = SimEvaluator(world).handle({"event": "status"}).result
    assert status["terminal"] in ("evaluation_failure", "collision")


def test_inverse_kinematics_reaches_its_target(sim: None) -> None:
    import numpy as np

    from uzumaki.sim import so101
    from uzumaki.sim.scene import build_model

    kin = so101.Kinematics(build_model(case_named("nominal"), assets.verify_assets()))
    target = (-0.12, -0.05, 0.03)
    q = kin.solve(target, math.radians(30))
    assert q is not None
    kin.data.qpos[kin.qpos] = q
    import mujoco

    mujoco.mj_kinematics(kin.model, kin.data)
    assert np.linalg.norm(kin.data.site_xpos[kin.site] - np.array(target)) < 1e-3
    frame = kin.data.site_xmat[kin.site].reshape(3, 3)
    assert frame[2, 0] < -0.999  # fingers point straight down
    assert kin.solve((0.0, 0.30, 0.03), 0.0) is None  # beyond reach
