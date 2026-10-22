"""Two effectors on one task: the ``handover`` pack, the ``handover-v1`` driver path and the
scripted handover world (``uzumaki.tasks``, ``uzumaki.baseline``, ``uzumaki.handover_env``).

Everything here is offline. The ALOHA case set (``src/uzumaki/sim/data/aloha_cases_v1.json``) is
plain data, so the scripted world runs it with no simulator; ``tests/test_aloha.py`` runs the
same cases in MuJoCo.
"""

import copy
import json
from pathlib import Path

import pytest

from uzumaki.baseline import (
    HANDOVER_SELECTOR_ID,
    HandoverSelector,
    correctness_failures,
    replay_case,
    run_case,
)
from uzumaki.embodiments import ALOHA, SO101
from uzumaki.fixture_env import HANDOVER_PROTOCOL, HANDOVER_TOOLS, FixtureClock, load_cases
from uzumaki.handover_env import HandoverWorld, handover_environment
from uzumaki.registry import load_skill_set, seal_skill
from uzumaki.robots import Arm, Gripper, Humanoid, Legs, Torso
from uzumaki.tasks import HANDOVER, TaskMismatch, task_pack

REPO = Path(__file__).resolve().parents[1]
CASES = load_cases(REPO / "src/uzumaki/sim/data/aloha_cases_v1.json", HANDOVER_PROTOCOL)


def snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    document = json.loads((REPO / "tests/fixtures/skills_handover_v1.json").read_text())
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(document))
    return load_skill_set(path, tools=HANDOVER.tool_catalog).snapshot()


# ---------------------------------------------------------------------- task pack
def test_handover_binds_a_giver_and_a_taker_to_two_arms() -> None:
    binding = HANDOVER.bind(ALOHA.body())
    assert dict(binding.roles) == {"giver": "left_gripper", "taker": "right_gripper"}
    assert dict(binding.claims) == {
        "observe": frozenset(),
        "pick": {"left"},
        "handover": {"left", "right"},
        "place": {"right"},
        "abort": {"left", "right"},
    }
    swapped = HANDOVER.bind(ALOHA.body(), effectors={"giver": "right_gripper"})
    assert dict(swapped.roles) == {"giver": "right_gripper", "taker": "left_gripper"}
    assert HANDOVER.tool_catalog == {tool: "1.0" for tool in HANDOVER_TOOLS}
    assert task_pack("handover") is HANDOVER


def test_handover_binds_to_any_body_with_two_grasping_effectors() -> None:
    humanoid = Humanoid(
        torso=Torso(("waist",)),
        left=Arm(("ls", "le")),
        right=Arm(("rs", "re")),
        left_hand=Gripper(("lh",)),
        right_hand=Gripper(("rh",)),
        legs=Legs((("lhip", "lknee"), ("rhip", "rknee"))),
    )
    assert set(HANDOVER.bind(humanoid.body()).roles.values()) == {"left_hand", "right_hand"}
    with pytest.raises(TaskMismatch, match="role 'taker'"):
        HANDOVER.bind(SO101.body())
    with pytest.raises(TaskMismatch, match="different effectors"):
        HANDOVER.bind(ALOHA.body(), effectors={"giver": "left_gripper", "taker": "left_gripper"})


# ---------------------------------------------------------------- scripted world
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_scripted_handover_meets_the_aloha_expectations_and_replays(case, tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = snapshot(tmp_path)
    run = run_case(case, registry, handover_environment, protocol=HANDOVER_PROTOCOL)
    assert correctness_failures(case, run) == []
    assert run.bundle["manifest"]["model_versions"] == {"planner": HANDOVER_SELECTOR_ID}
    report = replay_case(copy.deepcopy(run.bundle), registry, protocol=HANDOVER_PROTOCOL)
    assert report.matches, report.differences


def test_dropped_handover_is_recovered_by_picking_again(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.case_id == "dropped-handover")
    worlds: list[HandoverWorld] = []

    def environment(case):  # type: ignore[no-untyped-def]
        env = handover_environment(case)
        assert isinstance(env.world, HandoverWorld)
        worlds.append(env.world)
        return env

    run = run_case(case, snapshot(tmp_path), environment, protocol=HANDOVER_PROTOCOL)
    assert run.verdict is not None and run.verdict["primary"] == "completed"
    log = [(entry["tool"], entry["reason"]) for entry in worlds[0].log]
    assert log == [("pick", None), ("handover", None), ("pick", None), ("handover", None),
                   ("place", None)]  # fmt: skip


def test_the_taker_alone_may_place_and_only_the_giver_hands_over() -> None:
    world = HandoverWorld(CASES[0], FixtureClock())
    seen = world.observe({})
    assert seen["holder"] is None
    assert world.handover({"observation_id": seen["observation_id"]})["reason"] == "not_held"
    estimate = seen["object_pose_estimate"]
    world.pick({"observation_id": seen["observation_id"], "x_m": estimate["x_m"],
                "y_m": estimate["y_m"], "yaw_deg": estimate["yaw_deg"],
                "grasp_height_m": 0.015})  # fmt: skip
    seen = world.observe({})
    assert seen["holder"] == "giver" and seen["gripper_state"] == "holding"
    target = seen["target_pose"]
    place = {"observation_id": seen["observation_id"], **{k: target[k] for k in target}}
    assert world.place(place)["reason"] == "not_held"
    assert world.handover({"observation_id": seen["observation_id"]})["holder"] == "taker"
    seen = world.observe({})
    place["observation_id"] = seen["observation_id"]
    assert world.place(place)["status"] == "ok"
    assert world.audit()["admitted"] == {"pick": 1, "handover": 1, "place": 1, "abort": 0}


def test_handover_selector_follows_the_holder() -> None:
    eligible = [
        {"skill_id": name, "skill_version": 1} for name in ("pick-box", "hand-over", "place-box")
    ]

    def decide(state: str, holder: str | None) -> str:
        request = {
            "eligible": eligible,
            "observation": {"gripper_state": state, "holder": holder,
                            "target_pose": {"z_m": 0.015}},
        }  # fmt: skip
        return HandoverSelector().decide("d1", "planner", request)["skill_id"]

    assert decide("open", None) == "pick-box"
    assert decide("holding", "giver") == "hand-over"
    assert decide("holding", "taker") == "place-box"
