"""Task packs bind to any body that has what they need (``uzumaki.tasks``)."""

import pytest

from uzumaki.embodiments import EMBODIMENTS, SO101, embodiment
from uzumaki.planner import PICK_PLACE_CARD, SYSTEM_PROMPT, ModelPlanner, TaskCard
from uzumaki.robots import Arm, Camera, Gripper, Legs, Neck, Quadruped, QuadrupedWithJaw, TwoArms
from uzumaki.tasks import PICK_PLACE, TaskMismatch, task_pack

FOUR = Legs(tuple((f"hip_{i}", f"knee_{i}") for i in range(4)))


def test_pick_place_binds_to_a_single_arm() -> None:
    binding = PICK_PLACE.bind(SO101.body())
    assert binding.effector == "gripper"
    assert dict(binding.claims) == {
        "observe": frozenset(),
        "pick": {"arm"},
        "place": {"arm"},
        "abort": {"arm"},
    }
    assert PICK_PLACE.tool_catalog == {t: "1.0" for t in ("observe", "pick", "place", "abort")}


def test_the_same_pack_binds_to_other_bodies() -> None:
    jaw = QuadrupedWithJaw(legs=FOUR, neck=Neck(("pitch",)), jaw=Gripper(("jaw",)))
    assert PICK_PLACE.bind(jaw.body()).effector == "jaw"
    two = TwoArms(
        left=Arm(("l",)),
        right=Arm(("r",)),
        left_gripper=Gripper(("lg",)),
        right_gripper=Gripper(("rg",)),
    ).body()
    assert PICK_PLACE.bind(two, "right_gripper").claims["pick"] == {"right"}
    with pytest.raises(TaskMismatch, match="no effector"):
        PICK_PLACE.bind(Quadruped(legs=FOUR, camera=Camera("eye")).body())
    with pytest.raises(TaskMismatch, match="lacks"):
        PICK_PLACE.bind(two, "nose")


def test_registries() -> None:
    assert task_pack("pick-place") is PICK_PLACE
    assert set(EMBODIMENTS) >= {"fixture-arm", "so101"}
    assert embodiment("so101").robot is SO101
    with pytest.raises(KeyError, match="known"):
        embodiment("r2d2")


def test_planner_prompt_comes_from_the_task_card() -> None:
    assert PICK_PLACE_CARD.system_prompt() == SYSTEM_PROMPT
    card = TaskCard(
        intro="You are the planner of a quadruped harness for a fetch task.",
        eligibility_rule="- The harness checks applies_when for you.",
        goal="Bring the ball to the person.",
        observation_fields=("ball_seen",),
    )
    planner = ModelPlanner(None, "m", card=card)  # type: ignore[arg-type]
    assert planner.card.system_prompt().startswith(card.intro)
    assert "tabletop" not in planner.card.system_prompt()
