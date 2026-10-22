"""Unitree G1 with Dex3 hands as an ``ArmRig``: a humanoid on a stand working a table with its
left hand.

Stage one of the humanoid: the pelvis is fixed to a stand (the model's floating base joint is
removed), the legs hold the model's standing pose with their feet just off the floor, and the
waist and left arm (ten joints) place the hand. The right arm is drawn back, clear of the table.
The table is ``TABLE_HEIGHT_M`` high, a dining-table height that puts the tabletop at the G1's
hips.

The Dex3 hand pinches with its thumb against its index and middle fingers. The seven finger
joints follow one grip value (a synergy): ``GRIP_OPEN`` to ``GRIP_CLOSED`` on the index base
joint moves every joint in proportion from ``HAND_OPEN`` to ``HAND_CLOSED``. The middle finger is
coupled to the index finger, so the two act as one jaw, and flat pads on the fingertips and the
thumb tip grip the box, as on Spot. The rig adds ``left_tcp`` between thumb and fingertips, tilted
``TILT_DEG`` toward the fingers, so a top-down grasp comes down rolled onto the thumb side, where
thumb and fingertips meet at one height. Whether the hand holds the box is read from the thumb's
load.

The G1 reaches top-down grasps over the frozen ``pick-place-v1`` tabletop, so it runs that
geometry unchanged (``build_case_set``, like the Panda's and Spot's).
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from uzumaki.embodiments import G1
from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.robots import Arm, SingleArm
from uzumaki.sim import arm
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import G1_MODEL
from uzumaki.sim.world import FrameHook, SimWorld

CASES_FILE = Path(__file__).parent / "data" / "g1_cases_v1.json"
CASE_SET = "g1-dev-v1"
TABLE_HEIGHT_M = 0.75
FLOOR_Z_M = -TABLE_HEIGHT_M
#: Pelvis height above the floor with straight legs (the ``stand`` keyframe) and the clearance
#: left under the feet.
PELVIS_HEIGHT_M = 0.793
FOOT_CLEARANCE_M = 0.01
BASE_XY = (0.0, -0.38)
BASE_YAW_DEG = 90.0
ARM_JOINTS = (*G1.torso.joints, *G1.left.joints)
HAND_JOINTS = tuple(G1.left_hand.joints)
#: Left-hand joint positions open and at full grip, in ``HAND_JOINTS`` order (thumb 0-2, index
#: 0-1, middle 0-1). Open, thumb and fingertips stand about 9 cm apart either side of the grip
#: frame; closed, they have crossed it, so they meet on any box from 3 to 6 cm.
HAND_OPEN = (0.0, -0.43, 0.3, -0.79, -0.6, -0.79, -0.6)
HAND_CLOSED = (0.0, 0.75, 0.0, -1.57, -0.87, -1.57, -0.87)
GRIP_DRIVER = "left_hand_index_0_joint"
GRIP_OPEN = HAND_OPEN[3]
GRIP_CLOSED = HAND_CLOSED[3]
#: Joint torque limits (N m) for the model's otherwise unlimited 500 N m/rad position servos: the
#: thumb's balance the two coupled fingers, so a box closed on stays where it is. The hand holds
#: something when the thumb's load is above half its limit.
THUMB_TORQUE_NM = 2.0
FINGER_TORQUE_NM = 0.5
HOLDING_TORQUE_NM = 0.5 * THUMB_TORQUE_NM
#: The grip frame is tilted this far from the palm normal toward the fingers, so the hand comes
#: down rolled onto its thumb side: the thumb tip and the fingertips then meet at one height
#: instead of the thumb hanging below the fingers.
TILT_DEG = 20.0
#: The grip frame's origin on the wrist yaw link: between thumb and fingertips.
SITE_POS = (0.105, -0.09, 0.0)
#: Flat grip pads (child bodies, quaternions and centres in the parent link's frame, half sizes
#: along the grip frame's axes). The finger pad spans the index and middle fingertips, which move
#: as one (``customize`` couples the middle finger to the index finger), so a small box cannot
#: slip between them; the thumb pad faces it. Measured from the model half way through a grip.
FINGER_PAD = (
    "left_hand_index_1_link",
    (0.6711, -0.6711, -0.2227, 0.2227),
    (0.0431, -0.0019, -0.0285),
)
FINGER_PAD_SIZE = (0.015, 0.037, 0.004)
THUMB_PAD = ("left_hand_thumb_2_link", (0.5203, -0.5203, 0.4788, -0.4788), (0.0058, -0.0333, 0.0))
THUMB_PAD_SIZE = (0.012, 0.012, 0.004)
#: Rotor inertia for the light finger joints, as on Spot.
ARMATURE = 0.01
PAD_FRICTION = (1.0, 0.03, 0.0001)
PAD_SOLIMP = (0.99, 0.999, 0.001, 0.5, 2.0)
#: The right arm's resting pose: drawn back with the elbow bent, clear of the table however the
#: waist turns.
RIGHT_ARM_REST = (0.6, -0.2, 0.0, 1.6, 0.0, 0.0, 0.0)
READY = (-0.5, -0.07, 0.0, -0.06, 0.9, -0.14, -0.05, 0.75, -0.93, 0.02)
#: Joint motion per radian of jaw yaw near ``READY`` (measured): the redundant arm turns the
#: palm with the shoulder, elbow and wrist together.
JAW_TURN = (0.25, 0.4, -0.13, 0.16, -0.68, -0.06, 0.0, 0.29, 0.78, 0.0)


def _seeds(bearing: float, roll: float) -> list[Any]:
    # ``READY`` holds the jaws along world x; turn it toward the wanted jaw yaw.
    jaw = arm.wrap(roll + bearing + math.radians(BASE_YAW_DEG))
    turn = np.array(JAW_TURN)
    return [np.array(READY) + t * turn for t in (jaw, arm.wrap(jaw + math.pi))]


def customize(spec: Any) -> None:
    """Fix the pelvis to its stand, couple the fingers, add the grip pads and draw the stand."""
    spec.delete(spec.joint("floating_base_joint"))
    for pad, (body, quat, pos), size in (
        ("finger_pad", FINGER_PAD, FINGER_PAD_SIZE),
        ("thumb_pad", THUMB_PAD, THUMB_PAD_SIZE),
    ):
        holder = spec.body(body).add_body()
        holder.name = pad
        holder.quat = list(quat)
        holder.pos = list(pos)
        geom = holder.add_geom()
        geom.name = pad
        geom.type = mujoco.mjtGeom.mjGEOM_BOX
        geom.size = list(size)
        geom.mass = 0.001
        geom.condim = 4
        geom.friction = list(PAD_FRICTION)
        geom.solimp = list(PAD_SOLIMP)
        geom.group = 3
    for one, two in (
        ("box", "left_hand_index_1_link"),
        ("box", "left_hand_middle_1_link"),
        ("box", "left_hand_thumb_2_link"),
        ("finger_pad", "left_hand_middle_1_link"),
        ("finger_pad", "left_hand_middle_0_link"),
        # Over a small box the thumb closes onto the finger pad: the hand touching itself.
        ("finger_pad", "left_hand_thumb_2_link"),
        ("finger_pad", "thumb_pad"),
        # The wrist's roll and yaw links sit either side of the small pitch link; their meshes
        # overlap at wrist pitches the joint limits allow.
        ("left_wrist_roll_link", "left_wrist_yaw_link"),
    ):
        exclude = spec.add_exclude()
        exclude.bodyname1 = one
        exclude.bodyname2 = two
    for part in ("0", "1"):
        coupling = spec.add_equality()
        coupling.type = mujoco.mjtEq.mjEQ_JOINT
        coupling.name1 = f"left_hand_middle_{part}_joint"
        coupling.name2 = f"left_hand_index_{part}_joint"
    for name in HAND_JOINTS:
        spec.joint(name).armature = ARMATURE
    # The right hand's grip frame, mirroring the left's (the rig adds ``left_tcp``); the right
    # hand does not grip in this stage, but its embodiment names the site.
    right = spec.body("right_wrist_yaw_link").add_site()
    right.name = "right_tcp"
    right.pos = [SITE_POS[0], -SITE_POS[1], SITE_POS[2]]
    right.quat = list(_site_quat(-1.0))
    stand = spec.worldbody.add_geom()
    stand.name = "stand"
    stand.type = mujoco.mjtGeom.mjGEOM_CYLINDER
    height = PELVIS_HEIGHT_M + FOOT_CLEARANCE_M - 0.12
    stand.size = [0.025, height / 2, 0.0]
    stand.pos = [BASE_XY[0], BASE_XY[1] - 0.12, FLOOR_Z_M + height / 2]
    stand.rgba = [0.3, 0.32, 0.36, 1.0]
    stand.contype = stand.conaffinity = 0
    for name in HAND_JOINTS:
        limit = THUMB_TORQUE_NM if "thumb" in name else FINGER_TORQUE_NM
        finger = spec.actuator(name)
        finger.forcelimited = mujoco.mjtLimited.mjLIMITED_TRUE
        finger.forcerange = [-limit, limit]
    floor = spec.geom("floor")
    floor.pos = [0.0, 0.0, FLOOR_Z_M]
    floor.size = [1.5, 1.5, 0.05]
    # Visual only: table legs down to the floor and a brighter light for videos.
    for sx in (-1, 1):
        for sy in (-1, 1):
            leg = spec.worldbody.add_geom()
            leg.type = mujoco.mjtGeom.mjGEOM_BOX
            leg.size = [0.02, 0.02, (TABLE_HEIGHT_M - 0.02) / 2]
            leg.pos = [sx * 0.22, sy * 0.22, (FLOOR_Z_M - 0.02) / 2]
            leg.rgba = [0.45, 0.37, 0.3, 1.0]
            leg.contype = leg.conaffinity = 0
    spec.visual.headlight.ambient = [0.35, 0.35, 0.35]
    spec.visual.headlight.diffuse = [0.55, 0.55, 0.55]


def _site_quat(side: float = 1.0) -> tuple[float, float, float, float]:
    # Untilted, site x is out of the palm (the left hand's -y, the right hand's +y: ``side``
    # -1) and z along the hand's forward axis (its x); the tilt turns both about site y.
    tilt = math.radians(TILT_DEG)
    out = np.array([0.0, -side, 0.0])
    forward = np.array([1.0, 0.0, 0.0])
    x = math.cos(tilt) * out + math.sin(tilt) * forward
    z = -math.sin(tilt) * out + math.cos(tilt) * forward
    matrix = np.column_stack([x, np.cross(z, x), z])
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, matrix.flatten())
    return (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))


RIG = ArmRig(
    name="g1",
    robot=SingleArm("g1_left", model=G1.model, arm=Arm(ARM_JOINTS), gripper=G1.left_hand),
    assets=G1_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="pelvis",
    base_xy=BASE_XY,
    base_yaw_deg=BASE_YAW_DEG,
    base_z_m=FLOOR_Z_M + PELVIS_HEIGHT_M + FOOT_CLEARANCE_M,
    arm_actuators=ARM_JOINTS,
    gripper_actuator=GRIP_DRIVER,
    gripper_joint=GRIP_DRIVER,
    grip_open=GRIP_OPEN,
    grip_closed=GRIP_CLOSED,
    closed_below=0.0,
    opening_above=0.0,
    ready=READY,
    site="left_tcp",
    site_body="left_wrist_yaw_link",
    site_pos=SITE_POS,
    site_quat=_site_quat(),
    pad_bodies=("finger_pad", "thumb_pad", "left_hand_thumb_1_link"),
    pad_priority=None,
    fixed_jaw_offset_m=0.0,
    grasp_gap_m=0.0,
    min_site_z_m=0.02,
    seeds=_seeds,
    roll_index=8,
    pan_index=0,
    gravity_compensation=True,
    camera=(0.0, -0.3, 0.1, 1.6, -60.0, -20.0),
    customize=customize,
    world="uzumaki.sim.g1:G1World",
)


class G1World(SimWorld):
    """``SimWorld`` for the G1 on its stand: the hand follows one grip value, the legs and right
    arm hold their pose."""

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
        rig: ArmRig | None = None,
    ) -> None:
        self._hand = np.array([model.actuator(n).id for n in HAND_JOINTS])
        self._hand_qpos = np.array([model.jnt_qposadr[model.joint(n).id] for n in HAND_JOINTS])
        self._thumb = model.actuator("left_hand_thumb_1_joint").id
        self._open = np.array(HAND_OPEN)
        self._closed = np.array(HAND_CLOSED)
        right = tuple(G1.right.joints)
        self._rest = np.array([model.actuator(n).id for n in right])
        self._rest_qpos = np.array([model.jnt_qposadr[model.joint(n).id] for n in right])
        super().__init__(case, clock, model, frame_hook, rig or RIG)

    def _grip_value(self) -> float:
        return (float(self.data.ctrl[self._grip]) - GRIP_OPEN) / (GRIP_CLOSED - GRIP_OPEN)

    def _synergy(self) -> None:
        grip = self._grip_value()
        self.data.ctrl[self._hand] = self._open + grip * (self._closed - self._open)

    def _step(self, count: int) -> None:
        for _ in range(count):
            self._synergy()
            super()._step(1)

    def _reset(self) -> None:
        self.data.qpos[self._rest_qpos] = RIGHT_ARM_REST
        self.data.ctrl[self._rest] = RIGHT_ARM_REST
        self.kin.open_fingers = list(HAND_OPEN)
        self.data.ctrl[self._hand] = HAND_OPEN
        super()._reset()

    def _sync_kinematics(self) -> None:
        """Put the planning model in the pose the physics holds (right arm, legs, hand)."""
        self.kin.data.qpos[:] = self.data.qpos

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().pick(args)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().place(args)

    def _gripper_holding(self, arm: Any = None) -> bool:
        load = float(self.data.actuator_force[self._thumb])
        return self._grip_value() > 0.5 and load > HOLDING_TORQUE_NM


def build_case_set(baseline: Path, protocol: Path) -> dict[str, Any]:
    """The G1 case set from the frozen baseline cases and the protocol's development geometry
    (``uzumaki.baseline-cases/1.0``)."""
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    nominal = next(case for case in frozen["cases"] if case["case_id"] == "nominal")
    geometries = json.loads(protocol.read_text(encoding="utf-8"))["profile"]["development_cases"]
    cases = list(frozen["cases"])
    for geometry in geometries:
        case = copy.deepcopy(nominal)
        case.update(
            case_id=f"g1-{geometry['id']}",
            geometry=geometry["id"],
            box_size_m=geometry["box_size_m"],
            initial_xy_m=geometry["initial_xy_m"],
            initial_yaw_deg=geometry["initial_yaw_deg"],
            target_xy_m=geometry["target_xy_m"],
        )
        cases.append(case)
    return {"format": frozen["format"], "protocol": frozen["protocol"], "cases": cases}
