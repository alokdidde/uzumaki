"""Unitree Go1 with a jaw on its neck as an ``ArmRig``: a quadruped that grips with its head.

The Go1 stands on the floor in the model's ``home`` stance, its twelve leg joints held by their
position actuators, free to sway, as Spot does. It stands behind the tabletop's -y edge facing
+y. The Go1 has no neck, so ``customize`` builds one on the front of its trunk: six joints
(``NECK_JOINTS``: yaw, pitch, a second pitch half way along, then the head's pitch, turn and
roll), with the UMI gripper from MuJoCo Menagerie mounted on the end as the jaw. Six joints, not
five, because the body sways: with the trunk tilted a degree, five joints cannot hold the jaw
straight down at every yaw. The jaw's two fingers slide toward each other on one tendon
(``jaw/fingers_actuator``); the jaw holds something when the closed fingers stop short.

The table is ``TABLE_HEIGHT_M`` high, a low table at the dog's shoulder. The jaw reaches top-down
grasps over the frozen ``pick-place-v1`` tabletop, so the Go1 runs that geometry unchanged
(``build_case_set``, like the Panda's and Spot's). Only the lower legs may touch the floor.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from uzumaki.embodiments import GO1
from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.robots import Arm, SingleArm
from uzumaki.sim import arm
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import GO1_MODEL, UMI_MODEL
from uzumaki.sim.world import FrameHook, SimWorld

CASES_FILE = Path(__file__).parent / "data" / "go1_cases_v1.json"
CASE_SET = "go1-dev-v1"
TABLE_HEIGHT_M = 0.2
FLOOR_Z_M = -TABLE_HEIGHT_M
#: The ``home`` stance: trunk height above the floor and hip/knee angles of every leg.
STANCE_HEIGHT_M = 0.27
STANCE = (0.0, 0.9, -1.8)
LEGS = ("FR", "FL", "RR", "RL")
LEG_JOINTS = tuple(f"{leg}_{part}_joint" for leg in LEGS for part in ("hip", "thigh", "calf"))
NECK_JOINTS = tuple(GO1.neck.joints)
JAW_PREFIX = "jaw/"
FINGERS = (f"{JAW_PREFIX}left_finger_joint", f"{JAW_PREFIX}right_finger_joint")
JAW_ACTUATOR = f"{JAW_PREFIX}fingers_actuator"
BASE_XY = (0.0, -0.52)
BASE_YAW_DEG = 90.0
#: Neck mount on the trunk's top front (trunk frame), the link lengths (m), each link's mass and
#: the neck servos' stiffness (N m/rad) and torque limit (N m). The neck's weight is carried
#: through its servos by gravity compensation, so the legs still bear it.
MOUNT = (0.2, 0.0, 0.075)
UPPER_NECK_M = 0.28
LOWER_NECK_M = 0.26
HEAD_M = 0.09
NECK_LINK_MASS_KG = 0.3
NECK_KP = 1000.0
NECK_TORQUE_NM = 30.0
#: Jaw opening: the fingers' tendon is commanded from open (0) to closed. Closed on nothing,
#: each finger slides about 0.042 m; below ``HOLDING_BELOW_M`` it has stopped on something.
GRIP_OPEN = 0.0
GRIP_CLOSED = 0.05
HOLDING_BELOW_M = 0.036
#: The grip point between the finger pads, along the jaw's approach axis (m).
GRIP_DEPTH_M = 0.185
READY = (0.0, -1.54, 1.71, 1.37, 0.0, 1.57)


def _seeds(bearing: float, roll: float) -> list[Any]:
    # The head pitches the jaw straight down when the three pitches add up to pi/2; at head roll
    # pi/2 the jaw closes along the bearing. The jaw is symmetric, so a half turn is as good.
    turns = [arm.wrap(math.pi / 2 - roll), arm.wrap(-math.pi / 2 - roll)]
    return [
        np.array([bearing, pitch, elbow, math.pi / 2 - pitch - elbow, 0.0, turn])
        for pitch, elbow in ((-1.2, 1.8), (-0.5, 0.6), (-1.6, 2.5))
        for turn in turns
    ]


def _capsule(body: Any, length: float, radius: float = 0.022) -> None:
    geom = body.add_geom()
    geom.type = mujoco.mjtGeom.mjGEOM_CAPSULE
    geom.fromto = [0.0, 0.0, 0.0, length, 0.0, 0.0]
    geom.size = [radius, 0.0, 0.0]
    geom.mass = NECK_LINK_MASS_KG
    geom.rgba = [0.25, 0.25, 0.27, 1.0]


def _joint(body: Any, name: str, axis: tuple[float, float, float], limit: float) -> None:
    joint = body.add_joint()
    joint.name = name
    joint.axis = list(axis)
    joint.range = [-limit, limit]
    joint.limited = mujoco.mjtLimited.mjLIMITED_TRUE
    joint.armature = 0.01
    joint.damping = [1.0, 0.0, 0.0]
    joint.actgravcomp = True


def customize(spec: Any) -> None:
    """Build the neck, mount the UMI gripper on it as the jaw, and lower the floor."""
    yaw_name, pitch_name, elbow_name, head_name, turn_name, roll_name = NECK_JOINTS
    trunk = spec.body("trunk")
    neck = trunk.add_body()
    neck.name = "neck_base"
    neck.pos = list(MOUNT)
    _joint(neck, yaw_name, (0.0, 0.0, 1.0), 2.6)
    collar = neck.add_geom()
    collar.type = mujoco.mjtGeom.mjGEOM_CYLINDER
    collar.size = [0.035, 0.02, 0.0]
    collar.pos = [0.0, 0.0, 0.02]
    collar.mass = NECK_LINK_MASS_KG
    collar.rgba = [0.25, 0.25, 0.27, 1.0]
    upper = neck.add_body()
    upper.name = "upper_neck"
    upper.pos = [0.0, 0.0, 0.04]
    _joint(upper, pitch_name, (0.0, 1.0, 0.0), 2.0)
    _capsule(upper, UPPER_NECK_M)
    lower = upper.add_body()
    lower.name = "lower_neck"
    lower.pos = [UPPER_NECK_M, 0.0, 0.0]
    _joint(lower, elbow_name, (0.0, 1.0, 0.0), 2.6)
    _capsule(lower, LOWER_NECK_M)
    head = lower.add_body()
    head.name = "head"
    head.pos = [LOWER_NECK_M, 0.0, 0.0]
    _joint(head, head_name, (0.0, 1.0, 0.0), 2.0)
    _joint(head, turn_name, (0.0, 0.0, 1.0), 1.0)
    _capsule(head, HEAD_M, 0.03)
    jaw = head.add_body()
    jaw.name = "jaw_mount"
    jaw.pos = [HEAD_M, 0.0, 0.0]
    _joint(jaw, roll_name, (1.0, 0.0, 0.0), math.pi)
    wrist = jaw.add_geom()
    wrist.type = mujoco.mjtGeom.mjGEOM_CYLINDER
    wrist.size = [0.03, 0.01, 0.0]
    wrist.quat = [math.sqrt(0.5), 0.0, math.sqrt(0.5), 0.0]
    wrist.mass = 0.05
    wrist.rgba = [0.25, 0.25, 0.27, 1.0]
    for body in (neck, upper, lower, head, jaw):
        body.gravcomp = 1.0
    for name in NECK_JOINTS:
        actuator = spec.add_actuator()
        actuator.name = name
        actuator.target = name
        actuator.trntype = mujoco.mjtTrn.mjTRN_JOINT
        actuator.set_to_position(kp=NECK_KP, kv=2.0 * math.sqrt(NECK_KP) * 0.2)
        actuator.forcelimited = mujoco.mjtLimited.mjLIMITED_TRUE
        actuator.forcerange = [-NECK_TORQUE_NM, NECK_TORQUE_NM]
    umi = mujoco.MjSpec.from_file(str(UMI_MODEL.verify() / UMI_MODEL.entry))
    base = umi.worldbody.bodies[0]
    base.name = "base"
    base.pos = [0.0, 0.0, 0.0]
    base.quat = [1.0, 0.0, 0.0, 0.0]
    for joint in list(base.joints):
        umi.delete(joint)
    for actuator in list(umi.actuators):
        if actuator.name != "fingers_actuator":
            umi.delete(actuator)
    # Without implicit damping the stiff neck servos lag their targets by a centimetre at the jaw.
    # The jaw keeps the Go1's contact options.
    spec.option.integrator = mujoco.mjtIntegrator.mjINT_IMPLICITFAST
    umi.option.integrator = spec.option.integrator
    umi.option.impratio = spec.option.impratio
    umi.option.noslip_iterations = spec.option.noslip_iterations
    # The jaw's approach axis (UMI z) along the head, its fingers closing across it (UMI x).
    frame = jaw.add_frame(xyaxes=[0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
    spec.attach(umi, prefix=JAW_PREFIX, frame=frame)
    for name in ("base", "left_finger_holder", "right_finger_holder"):
        spec.body(f"{JAW_PREFIX}{name}").gravcomp = 1.0
    site = spec.body(f"{JAW_PREFIX}base").add_site()
    site.name = "tcp"
    site.pos = [0.0, 0.0, GRIP_DEPTH_M]
    # x along the approach (UMI z), z across the jaw (UMI x).
    site.quat = [0.0, math.sqrt(0.5), 0.0, math.sqrt(0.5)]
    floor = spec.geom("floor")
    floor.pos = [0.0, 0.0, FLOOR_Z_M]
    floor.size = [1.5, 1.5, 0.05]
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


RIG = ArmRig(
    name="go1",
    robot=SingleArm("go1_neck", model=GO1.model, arm=Arm(NECK_JOINTS), gripper=GO1.jaw),
    assets=GO1_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="trunk",
    base_xy=BASE_XY,
    base_yaw_deg=BASE_YAW_DEG,
    base_z_m=FLOOR_Z_M + STANCE_HEIGHT_M,
    arm_actuators=NECK_JOINTS,
    gripper_actuator=JAW_ACTUATOR,
    gripper_joint=FINGERS[0],
    grip_open=GRIP_OPEN,
    grip_closed=GRIP_CLOSED,
    closed_below=0.0,
    opening_above=0.0,
    ready=READY,
    site="tcp",
    site_body=None,
    site_pos=(0.0, 0.0, 0.0),
    site_quat=(1.0, 0.0, 0.0, 0.0),
    pad_bodies=(f"{JAW_PREFIX}left_finger_holder", f"{JAW_PREFIX}right_finger_holder"),
    pad_priority=None,
    fixed_jaw_offset_m=0.0,
    grasp_gap_m=0.0,
    min_site_z_m=0.022,
    seeds=_seeds,
    roll_index=5,
    pan_index=0,
    camera=(0.0, -0.3, 0.0, 1.4, -50.0, -20.0),
    customize=customize,
    world="uzumaki.sim.go1:Go1World",
)


class Go1World(SimWorld):
    """``SimWorld`` for the Go1 standing with its jaw: legs held in the stance, feet on the
    floor, planning follows the trunk wherever it has swayed to."""

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
        rig: ArmRig | None = None,
    ) -> None:
        self._legs_qpos = np.array([model.jnt_qposadr[model.joint(n).id] for n in LEG_JOINTS])
        # The model names each leg actuator after its joint without ``_joint``.
        legs = [n.removesuffix("_joint") for n in LEG_JOINTS]
        self._legs_ctrl = np.array([model.actuator(n).id for n in legs])
        self._stance = np.array(STANCE * len(LEGS))
        self._fingers = np.array([model.jnt_qposadr[model.joint(n).id] for n in FINGERS])
        lower = {model.body(f"{leg}_calf").id for leg in LEGS}
        self._feet = {g for g in range(model.ngeom) if int(model.geom_bodyid[g]) in lower}
        super().__init__(case, clock, model, frame_hook, rig or RIG)
        self.kin.robot_geoms -= self._feet

    def _reset(self) -> None:
        self.data.qpos[self._legs_qpos] = self._stance
        self.data.ctrl[self._legs_ctrl] = self._stance
        self.kin.open_fingers = [GRIP_OPEN, GRIP_OPEN]
        super()._reset()

    def _forbidden(self, a: int, b: int) -> bool:
        if {a, b} & self._feet and self._floor in (a, b):
            return False
        return super()._forbidden(a, b)

    def _sync_kinematics(self) -> None:
        self.kin.data.qpos[:] = self.data.qpos

    def _gripper_holding(self, arm: Any = None) -> bool:
        closed = float(self.data.ctrl[self._grip]) > (GRIP_OPEN + GRIP_CLOSED) / 2
        return closed and float(np.mean(self.data.qpos[self._fingers])) < HOLDING_BELOW_M

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().pick(args)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().place(args)


def build_case_set(baseline: Path, protocol: Path) -> dict[str, Any]:
    """The Go1 case set from the frozen baseline cases and the protocol's development geometry
    (``uzumaki.baseline-cases/1.0``)."""
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    nominal = next(case for case in frozen["cases"] if case["case_id"] == "nominal")
    geometries = json.loads(protocol.read_text(encoding="utf-8"))["profile"]["development_cases"]
    cases = list(frozen["cases"])
    for geometry in geometries:
        case = copy.deepcopy(nominal)
        case.update(
            case_id=f"go1-{geometry['id']}",
            geometry=geometry["id"],
            box_size_m=geometry["box_size_m"],
            initial_xy_m=geometry["initial_xy_m"],
            initial_yaw_deg=geometry["initial_yaw_deg"],
            target_xy_m=geometry["target_xy_m"],
        )
        cases.append(case)
    return {"format": frozen["format"], "protocol": frozen["protocol"], "cases": cases}
