"""Boston Dynamics Spot with its arm as an ``ArmRig``, standing on its four legs.

The body stands on the floor in the model's ``home`` stance, its twelve leg joints held by their
position actuators, and is free to sway: nothing fixes it to the world. It stands behind the
tabletop's -y edge facing +y (``BASE_XY``), body clear of the table, and works the tabletop with
its six-joint arm. Before every plan the planning model is moved to the pose the physics holds,
so inverse kinematics see the shoulder where it is, not where it started.

The gripper is Spot's: a fixed lower jaw on the wrist and a finger that swings down onto it
(``arm_f1x``, -1.57 open to 0 closed). The rig adds ``tcp`` near the jaw tips with x along the
approach and z from the fixed jaw toward the finger, so a top-down grasp turns the jaws sideways
and closes the finger across the box onto the fixed jaw. The box is gripped by two flat pads on
the jaw faces (the jaw collision meshes are convex hulls of curved parts and do not touch the
box). The finger's torque is limited to ``GRIP_TORQUE_NM`` instead of the model's 1000 N m, and
its load tells whether it holds something. The arm's weight is carried by its joint actuators
(gravity compensation through the actuators, so the legs still bear it).

Spot reaches top-down grasps over the frozen ``pick-place-v1`` tabletop, so like the Panda it runs
that geometry unchanged: ``CASES_FILE`` is the eight frozen fault cases plus nominal cases on the
twenty development geometries, rebuilt by ``build_case_set``. Only the lower legs may touch the
floor; any other robot contact with the floor or table is ``collision``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from uzumaki.embodiments import SPOT
from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.robots import SingleArm
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import SPOT_MODEL
from uzumaki.sim.world import FrameHook, SimWorld

CASES_FILE = Path(__file__).parent / "data" / "spot_cases_v1.json"
CASE_SET = "spot-dev-v1"
#: Table height above the floor (the scene's floor is 0.4 m below the tabletop).
FLOOR_Z_M = -0.4
#: The ``home`` stance: body height above the floor and hip/knee angles of every leg.
STANCE_HEIGHT_M = 0.46
STANCE = (0.0, 1.04, -1.8)
LEGS = ("fl", "fr", "hl", "hr")
LEG_JOINTS = tuple(f"{leg}_{part}" for leg in LEGS for part in ("hx", "hy", "kn"))
ARM_JOINTS = ("arm_sh0", "arm_sh1", "arm_el0", "arm_el1", "arm_wr0", "arm_wr1")
ARM_BODIES = (
    "arm_link_sh0",
    "arm_link_sh1",
    "arm_link_hr0",
    "arm_link_el0",
    "arm_link_el1",
    "arm_link_wr0",
    "arm_link_wr1",
    "arm_link_fngr",
)
BASE_XY = (0.0, -0.74)
BASE_YAW_DEG = 90.0
READY = (0.0, -1.2, 1.9, 0.0, 0.9, 0.0)
GRIP_OPEN = -1.2
GRIP_CLOSED = 0.0
#: Finger torque limit (N m): about 50 N at the box.
GRIP_TORQUE_NM = 5.0
#: Finger torque (N m) above which a closed gripper is holding something.
HOLDING_TORQUE_NM = 0.5 * GRIP_TORQUE_NM
#: Flat jaw pads (box half sizes and centres, metres): the fixed pad's face is the fixed jaw's
#: face, 0.036 m below the wrist axis; the finger pad's face is 6 mm above it when closed.
FIXED_PAD_SIZE = (0.043, 0.02, 0.004)
FIXED_PAD_POS = (0.193, 0.0, -0.040)
FINGER_PAD_SIZE = (0.045, 0.02, 0.004)
FINGER_PAD_POS = (0.078, 0.0, -0.0408)
ARMATURE = 0.01
PAD_FRICTION = (1.0, 0.03, 0.0001)
#: Hard pad contacts, so a squeezed box keeps its size.
PAD_SOLIMP = (0.99, 0.999, 0.001, 0.5, 2.0)


def _seeds(bearing: float, roll: float) -> list[Any]:
    return [
        # arm_wr1 turns the jaws clockwise seen from above when the wrist points down.
        np.array([bearing, sh1, el0, 0.0, wr0, -roll])
        for sh1, el0, wr0 in (
            (-1.2, 1.9, 0.8),
            (-0.8, 1.5, 0.9),
            (-1.6, 2.4, 0.8),
            (-0.5, 1.0, 1.0),
        )
    ]


def customize(spec: Any) -> None:
    """Jaw pads, finger force limit, arm gravity compensation and rotor inertia."""
    # The jaw collision meshes are convex hulls of curved parts; the box is gripped by two flat
    # pads on the jaw faces instead (the meshes still collide with everything else).
    for body, pad, size, pos in (
        ("arm_link_wr1", "fixed_pad", FIXED_PAD_SIZE, FIXED_PAD_POS),
        ("arm_link_fngr", "finger_pad", FINGER_PAD_SIZE, FINGER_PAD_POS),
    ):
        holder = spec.body(body).add_body()
        holder.name = pad
        geom = holder.add_geom()
        geom.name = pad
        geom.type = mujoco.mjtGeom.mjGEOM_BOX
        geom.size = list(size)
        geom.pos = list(pos)
        geom.mass = 0.001
        geom.condim = 4
        geom.friction = list(PAD_FRICTION)
        geom.solimp = list(PAD_SOLIMP)
        geom.group = 3
        exclude = spec.add_exclude()
        exclude.bodyname1 = "box"
        exclude.bodyname2 = body
    for name in ARM_BODIES:
        spec.body(name).gravcomp = 1.0
    for name in ARM_JOINTS:
        spec.joint(name).actgravcomp = True
    # Rotor inertia the model leaves out: without it the light finger and wrist chatter
    # against the box at the 5 ms step.
    for name in (*LEG_JOINTS, *ARM_JOINTS, "arm_f1x"):
        spec.joint(name).armature = ARMATURE
    finger = spec.actuator("arm_f1x")
    finger.forcelimited = mujoco.mjtLimited.mjLIMITED_TRUE
    finger.forcerange = [-GRIP_TORQUE_NM, GRIP_TORQUE_NM]
    floor = spec.geom("floor")
    floor.size = [2.0, 2.0, 0.05]
    # Visual only: table legs down to the floor and a brighter light for videos.
    for sx in (-1, 1):
        for sy in (-1, 1):
            leg = spec.worldbody.add_geom()
            leg.type = mujoco.mjtGeom.mjGEOM_BOX
            leg.size = [0.02, 0.02, (-FLOOR_Z_M - 0.02) / 2]
            leg.pos = [sx * 0.22, sy * 0.22, (FLOOR_Z_M - 0.02) / 2]
            leg.rgba = [0.45, 0.37, 0.3, 1.0]
            leg.contype = leg.conaffinity = 0
    spec.visual.headlight.ambient = [0.35, 0.35, 0.35]
    spec.visual.headlight.diffuse = [0.55, 0.55, 0.55]


RIG = ArmRig(
    name="spot",
    robot=SingleArm("spot_arm", model=SPOT.model, arm=SPOT.arm, gripper=SPOT.gripper),
    assets=SPOT_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="body",
    base_xy=BASE_XY,
    base_yaw_deg=BASE_YAW_DEG,
    base_z_m=FLOOR_Z_M + STANCE_HEIGHT_M,
    arm_actuators=ARM_JOINTS,
    gripper_actuator="arm_f1x",
    gripper_joint="arm_f1x",
    grip_open=GRIP_OPEN,
    grip_closed=GRIP_CLOSED,
    closed_below=0.0,
    opening_above=0.0,
    ready=READY,
    site="tcp",
    site_body="arm_link_wr1",
    site_pos=(0.215, 0.0, 0.0),
    site_quat=(1.0, 0.0, 0.0, 0.0),
    pad_bodies=("fixed_pad", "finger_pad"),
    pad_priority=None,
    fixed_jaw_offset_m=0.036,
    grasp_gap_m=0.005,
    min_site_z_m=0.036,
    seeds=_seeds,
    roll_index=5,
    pan_index=0,
    camera=(0.0, -0.35, -0.05, 1.7, -40.0, -20.0),
    customize=customize,
    world="uzumaki.sim.spot:SpotWorld",
)


class SpotWorld(SimWorld):
    """``SimWorld`` on a robot that stands: legs held in the stance, feet may touch the floor,
    and planning follows the body wherever it has swayed to."""

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
        rig: ArmRig | None = None,
    ) -> None:
        self._legs_qpos = np.array([model.jnt_qposadr[model.joint(n).id] for n in LEG_JOINTS])
        self._legs_ctrl = np.array([model.actuator(n).id for n in LEG_JOINTS])
        self._stance = np.array(STANCE * len(LEGS))
        lower = {model.body(f"{leg}_lleg").id for leg in LEGS}
        #: Lower legs: the only robot geoms allowed on the floor. The model's feet are soft
        #: spheres that sink a few centimetres, so the bottom of the lower leg touches down too.
        self._feet = {g for g in range(model.ngeom) if int(model.geom_bodyid[g]) in lower}
        super().__init__(case, clock, model, frame_hook, rig or RIG)
        # The planner checks the arm against the table and floor; the feet stand on the floor.
        self.kin.robot_geoms -= self._feet

    def _reset(self) -> None:
        self.data.qpos[self._legs_qpos] = self._stance
        self.data.ctrl[self._legs_ctrl] = self._stance
        super()._reset()

    def _forbidden(self, a: int, b: int) -> bool:
        if {a, b} & self._feet and self._floor in (a, b):
            return False
        return super()._forbidden(a, b)

    def _sync_kinematics(self) -> None:
        """Put the planning model in the pose the physics holds (body, legs and arm)."""
        self.kin.data.qpos[:] = self.data.qpos

    def _gripper_holding(self, arm: Any = None) -> bool:
        # A small box sits in the pocket by the hinge, where the finger closes almost fully:
        # the finger's load tells whether something stops it, as Spot's gripper reports.
        closed = float(self.data.ctrl[self._grip]) > (GRIP_OPEN + GRIP_CLOSED) / 2
        return closed and float(self.data.actuator_force[self._grip]) > HOLDING_TORQUE_NM

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().pick(args)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sync_kinematics()
        return super().place(args)


def build_case_set(baseline: Path, protocol: Path) -> dict[str, Any]:
    """The Spot case set from the frozen baseline cases and the protocol's development geometry
    (``uzumaki.baseline-cases/1.0``)."""
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    nominal = next(case for case in frozen["cases"] if case["case_id"] == "nominal")
    geometries = json.loads(protocol.read_text(encoding="utf-8"))["profile"]["development_cases"]
    cases = list(frozen["cases"])
    for geometry in geometries:
        case = copy.deepcopy(nominal)
        case.update(
            case_id=f"spot-{geometry['id']}",
            geometry=geometry["id"],
            box_size_m=geometry["box_size_m"],
            initial_xy_m=geometry["initial_xy_m"],
            initial_yaw_deg=geometry["initial_yaw_deg"],
            target_xy_m=geometry["target_xy_m"],
        )
        cases.append(case)
    return {"format": frozen["format"], "protocol": frozen["protocol"], "cases": cases}
