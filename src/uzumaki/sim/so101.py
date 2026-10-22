"""The SO-101 as an ``ArmRig``: top-down grasps with five joints and a one-joint gripper.

The SO-101 arm has five joints (pan, lift, elbow, wrist flex, wrist roll). A top-down grasp fixes
the approach and the jaw yaw, which together with the position is exactly what five joints can
reach. Its gripper is asymmetric: the moving jaw pushes the box onto the fixed jaw, so grasps are
planned with the fixed jaw ``GRASP_GAP_M`` short of the box face. Kinematics and planning are the
shared ones of ``uzumaki.sim.arm``; this module keeps the names the SO-101 code has always used.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from uzumaki.embodiments import SO101
from uzumaki.sim import arm
from uzumaki.sim.arm import ArmRig, Grasp, Placement, jaw_frame, wrap
from uzumaki.sim.assets import SO101_MODEL
from uzumaki.sim.cases import BASE_XY, BASE_YAW_DEG, CASE_SET, CASES_FILE

__all__ = ["Grasp", "Placement", "jaw_frame", "wrap"]

#: Names from the SO-101's ``SingleArm`` description (``uzumaki.embodiments.SO101``).
ARM_JOINTS: tuple[str, ...] = SO101.arm.joints
GRIPPER_JOINT: str = SO101.gripper.joints[0]
SITE: str = SO101.gripper.site or "gripperframe"
#: Raised pose the arm starts in and passes through before every approach.
READY = (0.0, -0.84, -0.73, 1.58, 0.0)
GRIP_OPEN = 1.2
GRIP_CLOSED = -0.2
#: Gripper joint position above which a closed gripper is holding something.
HOLDING_THRESHOLD = 0.05
#: Fixed-jaw tip offset from the site along the jaw axis (metres, measured on the model).
FIXED_JAW_OFFSET_M = 0.0201
GRASP_GAP_M = 0.003
#: Lowest site height for a grasp or place, keeping the fingertips off the tabletop.
MIN_SITE_Z_M = 0.012
HOVER_HEIGHTS_M = arm.HOVER_HEIGHTS_M
CARRY_HEIGHTS_M = arm.CARRY_HEIGHTS_M


def _seeds(bearing: float, roll: float) -> list[Any]:
    # The pan joint turns the arm clockwise seen from above, hence the sign.
    return [
        np.array([-bearing, lift, elbow, flex, roll])
        for lift, elbow, flex in ((0.0, 0.3, 1.2), (-0.5, 1.0, 1.2), (0.5, -0.3, 1.4))
    ]


RIG = ArmRig(
    name="so101",
    robot=SO101,
    assets=SO101_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="base",
    base_xy=BASE_XY,
    base_yaw_deg=BASE_YAW_DEG,
    base_z_m=0.0,
    arm_actuators=ARM_JOINTS,
    gripper_actuator=GRIPPER_JOINT,
    gripper_joint=GRIPPER_JOINT,
    grip_open=GRIP_OPEN,
    grip_closed=GRIP_CLOSED,
    closed_below=HOLDING_THRESHOLD,
    opening_above=HOLDING_THRESHOLD,
    ready=READY,
    site=SITE,
    site_body=None,
    site_pos=(0.0, 0.0, 0.0),
    site_quat=(1.0, 0.0, 0.0, 0.0),
    pad_bodies=("gripper", "moving_jaw_so101_v1"),
    pad_priority=1,
    fixed_jaw_offset_m=FIXED_JAW_OFFSET_M,
    grasp_gap_m=GRASP_GAP_M,
    min_site_z_m=MIN_SITE_Z_M,
    seeds=_seeds,
    roll_index=4,
    pan_index=0,
)


class Kinematics(arm.Kinematics):
    """``uzumaki.sim.arm.Kinematics`` for the SO-101 rig."""

    def __init__(self, model: Any, rig: ArmRig = RIG) -> None:
        super().__init__(model, rig)


def ready_toward(pose: Any) -> Any:
    """``READY`` with the pan and wrist roll of ``pose``: where an approach starts from."""
    ready = np.array(READY)
    ready[0], ready[4] = pose[0], pose[4]
    return ready


def site_position(center: Sequence[float], jaw_yaw: float, box_size: float) -> Any:
    """Site position that leaves the fixed jaw ``GRASP_GAP_M`` from the box face."""
    return arm.site_position(RIG, center, jaw_yaw, box_size)


plan_grasp = arm.plan_grasp
plan_place = arm.plan_place
