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


@pytest.mark.parametrize("case_id", ["nominal", "failed-grasp", "unreachable-goal"])
def test_model_planner_drives_the_so101(sim: None, case_id: str, tmp_path: Path) -> None:
    """A (fake) model chooses the skills; recorded exchanges replay the same run offline."""
    from model_fakes import CompetentModel
    from uzumaki.baseline import BASELINE_SKILL_BODIES, MODEL_BUDGETS
    from uzumaki.gateway import RecordingProvider
    from uzumaki.planner import ModelPlanner
    from uzumaki.providers import RecordedProvider
    from uzumaki.sim.run import run_sim_case

    registry = snapshot()
    case = case_named(case_id)

    def run_with(provider: Any) -> Any:
        planner = ModelPlanner(registry, "fake/model", frozenset(BASELINE_SKILL_BODIES))
        run, _world = run_sim_case(
            case,
            registry,
            selector=planner,
            planner_id=planner.model_version,
            budgets=MODEL_BUDGETS,
            provider=provider,
        )
        return run

    recording = RecordingProvider(CompetentModel())
    live = run_with(recording)
    assert correctness_failures(case, live, model_calls=None) == []
    assert live.report.usage["model_calls"]["used"] >= 1
    path = tmp_path / "exchanges.json"
    path.write_text(json.dumps(recording.document()), encoding="utf-8")
    again = run_with(RecordedProvider.from_path(path))
    assert json.dumps(again.bundle, sort_keys=True) == json.dumps(live.bundle, sort_keys=True)


def test_generated_skill_is_repaired_and_confirmed_on_the_so101(
    sim: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator CLI end to end with a scripted model in place of the gateway: three rounds on
    the 16 development cases, then the frozen program on the 12 confirmation cases."""
    from generated_programs import NO_RESUME, STALE_PLACE, TRANSFER, ScriptedGenerator
    from uzumaki import backends
    from uzumaki.sandbox import ContainmentUnavailable, admit, probe_platform
    from uzumaki.sim import generate

    try:
        admit(probe_platform())
    except ContainmentUnavailable as error:
        if os.environ.get("UZUMAKI_REQUIRE_CONTAINMENT") == "1":
            raise
        pytest.skip(f"unverified platform for the restricted worker: {error}")
    model = ScriptedGenerator([STALE_PLACE, NO_RESUME, TRANSFER])
    monkeypatch.setattr(backends, "GatewayProvider", lambda **_: model)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "not-a-key")
    out, record = tmp_path / "out", tmp_path / "exchanges.json"
    assert generate.main(["--out", str(out), "--record", str(record)]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    generation = summary["generation"]
    assert generation["status"] == "accepted"
    assert generation["accepted"] == "transfer-box@3"
    assert [p["passed"] for p in generation["programs"]] == [3, 15, 16]
    assert generation["programs"][1]["failed"] == {"stale-after-pick": "planning"}
    confirmation = summary["confirmation"]
    assert [row["case_id"] for row in confirmation] == CASE_IDS[16:]
    assert all(row["passed"] and row["violations"] == 0 for row in confirmation)
    sent = json.dumps(model.requests)
    for case_id in CASE_IDS[16:]:
        assert case_id not in sent  # confirmation cases never reach the generator
    assert (out / "round-3.py").read_text(encoding="utf-8") == TRANSFER.strip("\n") + "\n"
    assert len(list((out / "round-1").glob("*.json"))) == 16
    assert len(list((out / "confirmation").glob("*.json"))) == 12
    assert len(json.loads(record.read_text(encoding="utf-8"))["records"]) == 3


def test_one_command_demo_closes_the_loop_on_the_so101(
    sim: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m uzumaki.sim.demo`` with a scripted model for both roles: the planner abstains
    for lack of a skill, the generator writes and repairs one, it is confirmed and promoted, and
    the planner then completes the same task with it from the saved library."""
    from generated_programs import NO_RESUME, STALE_PLACE, TRANSFER, DemoModel
    from uzumaki import backends
    from uzumaki.promotion import load_library
    from uzumaki.sandbox import ContainmentUnavailable, admit, probe_platform
    from uzumaki.sim import demo

    try:
        admit(probe_platform())
    except ContainmentUnavailable as error:
        if os.environ.get("UZUMAKI_REQUIRE_CONTAINMENT") == "1":
            raise
        pytest.skip(f"unverified platform for the restricted worker: {error}")
    model = DemoModel([STALE_PLACE, NO_RESUME, TRANSFER])
    monkeypatch.setattr(backends, "AnthropicProvider", lambda **_: model)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    out = tmp_path / "demo"
    assert demo.main(["--out", str(out), "--provider", "anthropic"]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["task"] == demo.DEFAULT_CASE
    assert summary["before"]["primary"] == "abstained"
    assert summary["before"]["decisions"][0]["action"] == "abstain"
    assert summary["generation"]["accepted"] == "transfer-box@3"
    assert all(row["passed"] for row in summary["confirmation"])
    assert summary["promotion"]["evidence_ref"] == "promotion.transfer-box.3"
    after = summary["after"]
    assert (after["primary"], after["status"], after["violations"]) == ("completed", "completed", 0)
    assert after["decisions"][0]["skill_id"] == "transfer-box"
    assert summary["result"] == "completed"
    registry, bodies = load_library(out / "library")
    assert registry.state("transfer-box", 3) == "validated"
    assert list(bodies) == ["transfer-box@3"]
    generator_requests = [r for r in model.requests if r["role"] == "generator"]
    assert demo.DEFAULT_CASE not in json.dumps(generator_requests)


def test_so101_is_a_conformant_single_arm(sim: None) -> None:
    """The SO-101 is described as a ``SingleArm``; its names match the model file, and the
    pick-place pack binds to it, resets deterministically, replays and is judged by the
    evaluator."""
    from uzumaki import conformance
    from uzumaki.embodiments import embodiment
    from uzumaki.tasks import PICK_PLACE

    results = conformance.check(embodiment("so101"), PICK_PLACE, "so101-dev-009")
    assert {r.name: r.status for r in results} == {
        name: "pass"
        for name in (
            "body",
            "model_names",
            "task_binding",
            "skill_catalog",
            "stop_preempts",
            "deterministic_reset",
            "replay",
            "evaluator_outcome",
        )
    }
