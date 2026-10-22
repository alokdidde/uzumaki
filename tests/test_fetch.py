"""A body that moves: the ``fetch`` pack, the ``fetch-v1`` driver path and the scripted fetch world
(``uzumaki.tasks``, ``uzumaki.baseline``, ``uzumaki.fetch_env``).

Everything here is offline. The TidyBot case set (``src/uzumaki/sim/data/tidybot_cases_v1.json``)
is plain data, so the scripted world runs it with no simulator; ``tests/test_tidybot.py`` runs the
same cases in MuJoCo.
"""

import copy
import json
from pathlib import Path

import pytest

from uzumaki.baseline import (
    FETCH_SELECTOR_ID,
    FetchSelector,
    correctness_failures,
    replay_case,
    run_case,
)
from uzumaki.embodiments import ALOHA, SO101, TIDYBOT
from uzumaki.fetch_env import FetchWorld, fetch_environment, station, table_under
from uzumaki.fixture_env import FETCH_PROTOCOL, FETCH_TOOLS, FixtureClock, load_cases
from uzumaki.registry import load_skill_set, seal_skill
from uzumaki.robots import Gripper, Legs, Neck, QuadrupedWithJaw
from uzumaki.tasks import FETCH, TaskMismatch, task_pack

REPO = Path(__file__).resolve().parents[1]
CASES = load_cases(REPO / "src/uzumaki/sim/data/tidybot_cases_v1.json", FETCH_PROTOCOL)


def snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    document = json.loads((REPO / "tests/fixtures/skills_fetch_v1.json").read_text())
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(document))
    return load_skill_set(path, tools=FETCH.tool_catalog).snapshot()


# ---------------------------------------------------------------------- task pack
def test_fetch_binds_the_base_to_go_to_and_the_arm_to_pick_and_place() -> None:
    binding = FETCH.bind(TIDYBOT.body())
    assert binding.effector == "gripper"
    assert dict(binding.claims) == {
        "observe": frozenset(),
        "go_to": {"base"},
        "pick": {"arm"},
        "place": {"arm"},
        "abort": {"arm", "base"},
    }
    assert FETCH.tool_catalog == {tool: "1.0" for tool in FETCH_TOOLS}
    assert task_pack("fetch") is FETCH


def test_fetch_needs_a_body_that_moves() -> None:
    dog = QuadrupedWithJaw(
        legs=Legs(tuple((f"hip_{i}", f"knee_{i}") for i in range(4))),
        neck=Neck(("neck_pitch",)),
        jaw=Gripper(("jaw",), kind="jaw"),
    )
    assert FETCH.bind(dog.body()).claims["go_to"] == {f"legs_{i}" for i in range(1, 5)}
    with pytest.raises(TaskMismatch, match="locomote"):
        FETCH.bind(SO101.body())
    with pytest.raises(TaskMismatch, match="locomote"):
        FETCH.bind(ALOHA.body())


# ---------------------------------------------------------------- scripted world
@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_scripted_fetch_meets_the_tidybot_expectations_and_replays(case, tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = snapshot(tmp_path)
    run = run_case(case, registry, fetch_environment, protocol=FETCH_PROTOCOL)
    assert correctness_failures(case, run) == []
    assert run.bundle["manifest"]["model_versions"] == {"planner": FETCH_SELECTOR_ID}
    report = replay_case(copy.deepcopy(run.bundle), registry, protocol=FETCH_PROTOCOL)
    assert report.matches, report.differences


def test_a_drive_that_stops_short_is_driven_again(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.case_id == "stopped-short")
    worlds: list[FetchWorld] = []

    def environment(case):  # type: ignore[no-untyped-def]
        env = fetch_environment(case)
        assert isinstance(env.world, FetchWorld)
        worlds.append(env.world)
        return env

    run = run_case(case, snapshot(tmp_path), environment, protocol=FETCH_PROTOCOL)
    assert run.verdict is not None and run.verdict["primary"] == "completed"
    log = [(entry["tool"], entry["reason"]) for entry in worlds[0].log]
    assert log == [("go_to", None), ("go_to", None), ("pick", None), ("go_to", None),
                   ("place", None)]  # fmt: skip
    assert run.report.usage["attempts"]["used"] == 2


def test_pick_and_place_only_within_reach_and_go_to_only_onto_a_table() -> None:
    world = FetchWorld(CASES[0], FixtureClock())
    seen = world.observe({})
    assert seen["within_reach"] == {"object": False, "target": False}
    estimate = seen["object_pose_estimate"]
    pick = {
        "observation_id": seen["observation_id"],
        **{key: estimate[key] for key in ("x_m", "y_m", "yaw_deg")},
        "grasp_height_m": 0.02,
    }
    assert world.pick(pick)["reason"] == "unreachable_target"
    between = {"observation_id": seen["observation_id"], "x_m": 0.4, "y_m": 0.0}
    assert world.go_to(between)["reason"] == "unreachable_target"
    reply = world.go_to({**between, "x_m": estimate["x_m"], "y_m": estimate["y_m"]})
    assert reply["status"] == "ok"
    seen = world.observe({})
    assert seen["within_reach"] == {"object": True, "target": False}
    assert seen["base_pose"]["yaw_deg"] == 90.0
    assert world.pick({**pick, "observation_id": seen["observation_id"]})["status"] == "ok"
    seen = world.observe({})
    target = seen["target_pose"]
    place = {"observation_id": seen["observation_id"], **{k: target[k] for k in target}}
    assert world.place(place)["reason"] == "unreachable_target"
    assert world.audit()["admitted"] == {"pick": 1, "go_to": 1, "place": 0, "abort": 0}


def test_stations_face_the_table_under_the_point() -> None:
    assert table_under(0.0, 0.0) == "a" and table_under(0.8, 0.1) == "b"
    assert table_under(0.4, 0.0) is None and station(0.4, 0.0) is None
    assert station(0.1, 0.1) == (0.1, -0.52, 90.0)


def test_fetch_selector_goes_to_what_is_out_of_reach() -> None:
    eligible = [
        {"skill_id": name, "skill_version": 1} for name in ("go-to", "pick-box", "place-box")
    ]

    def decide(state: str, reach: dict[str, bool] | None) -> dict:  # type: ignore[type-arg]
        request = {
            "eligible": eligible,
            "observation": {"gripper_state": state, "within_reach": reach,
                            "target_pose": {"z_m": 0.02}},
        }  # fmt: skip
        return FetchSelector().decide("d1", "planner", request)

    far = {"object": False, "target": False}
    assert decide("open", far)["parameters"] == {"to": "object"}
    assert decide("open", {"object": True, "target": False})["skill_id"] == "pick-box"
    assert decide("holding", {"object": True, "target": False})["parameters"] == {"to": "target"}
    assert decide("holding", {"object": True, "target": True})["skill_id"] == "place-box"
    assert decide("open", None)["action"] == "abstain"
