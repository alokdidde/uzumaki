"""Components and configurations (``uzumaki.robots``) compile to the body model."""

import pytest

from uzumaki.body import BodyError
from uzumaki.embodiments import FIXTURE_ARM, SO101
from uzumaki.robots import (
    CONFIGURATIONS,
    Arm,
    Camera,
    Gripper,
    Hand,
    Head,
    Humanoid,
    Legs,
    MobileManipulator,
    Neck,
    Quadruped,
    QuadrupedWithArm,
    QuadrupedWithJaw,
    Robot,
    Torso,
    TwoArms,
    WheeledBase,
)

FOUR = Legs(tuple((f"hip_{i}", f"knee_{i}") for i in range(4)))


def caps(robot: Robot) -> dict[str, tuple[str, ...]]:
    body = robot.body()
    return {c.key: c.parts for c in body.capabilities}


def test_single_arm_is_a_few_lines_and_exposes_handles() -> None:
    body = SO101.body()
    assert body.configuration == "single_arm" and SO101.handles == ("arm", "gripper")
    assert SO101.arm.joints[0] == "shoulder_pan" and SO101.gripper.site == "gripperframe"
    assert caps(SO101) == {"reach@gripper": ("arm",), "grasp@gripper": ("arm",)}
    assert FIXTURE_ARM.body().effectors_with({"reach", "grasp"}) == ("gripper",)


def test_two_arms_reach_independently_on_a_rigid_torso() -> None:
    robot = TwoArms(
        left=Arm(("l1", "l2")),
        right=Arm(("r1", "r2")),
        left_gripper=Gripper(("lg",)),
        right_gripper=Gripper(("rg",)),
        torso=Torso(),
        camera=Camera("overhead"),
    )
    found = caps(robot)
    assert found["reach@left_gripper"] == ("left",) and found["reach@right_gripper"] == ("right",)
    assert robot.body().effectors_with({"reach", "grasp"}) == ("left_gripper", "right_gripper")


def test_humanoid_waist_is_shared_by_both_arms() -> None:
    robot = Humanoid(
        torso=Torso(("waist",)),
        left=Arm(("ls",)),
        right=Arm(("rs",)),
        left_hand=Hand(("lh",)),
        right_hand=Hand(("rh",)),
        legs=Legs((("lhip",), ("rhip",))),
        head=Head(("pan",)),
        camera=Camera("eye"),
    )
    found = caps(robot)
    assert found["locomote"] == ("legs_1", "legs_2")
    assert found["reach@left_hand"] == ("left", "torso")
    assert robot.body().effectors[0].kind == "hand"
    assert robot.body().sensors[0].part == "head"


def test_quadruped_with_jaw_reaches_with_its_neck() -> None:
    robot = QuadrupedWithJaw(
        legs=FOUR, neck=Neck(("pitch", "yaw")), jaw=Gripper(("jaw",)), camera=Camera("eye")
    )
    body = robot.body()
    assert caps(robot)["reach@jaw"] == ("neck",)
    assert body.effectors[0].kind == "jaw" and body.effectors_with({"reach", "grasp"}) == ("jaw",)
    assert set(body.claims("locomote")).isdisjoint(body.claims("reach", "jaw"))


def test_other_configurations_compile() -> None:
    arm, gripper = Arm(("a1", "a2")), Gripper(("g",))
    assert caps(Quadruped(legs=FOUR, camera=Camera("eye")))["locomote"][0] == "legs_1"
    spot = QuadrupedWithArm(legs=FOUR, arm=arm, gripper=gripper)
    assert spot.body().capability("reach", "gripper").parts == ("arm",)
    mobile = MobileManipulator(
        base=WheeledBase(("left_wheel", "right_wheel")), arm=arm, gripper=gripper
    )
    assert caps(mobile)["locomote"] == ("base",)
    assert set(CONFIGURATIONS) == {
        "single_arm",
        "two_arms",
        "mobile_manipulator",
        "humanoid",
        "quadruped",
        "quadruped_with_arm",
        "quadruped_with_jaw",
    }


def test_custom_bodies_and_mistakes() -> None:
    hexapod = Robot(
        "hexapod",
        body=Torso(),
        legs=Legs(tuple((f"c{i}", f"f{i}") for i in range(6)), on="body"),
        mandible=Gripper(("m",), on="body", kind="jaw"),
    )
    found = caps(hexapod)
    assert "reach@mandible" not in found and found["grasp@mandible"] == ("body",)
    with pytest.raises(BodyError, match="which part"):
        Robot("x", left=Arm(("a",)), right=Arm(("b",)), gripper=Gripper(("g",))).body()
    with pytest.raises(BodyError, match="cannot mount on legs"):
        Robot("x", legs=FOUR, arm=Arm(("a",), on="legs")).body()
    with pytest.raises(BodyError, match="four legs"):
        Quadruped(legs=Legs((("a",), ("b",))))
    with pytest.raises(BodyError, match="not a component"):
        Robot("x", arm="shoulder")
