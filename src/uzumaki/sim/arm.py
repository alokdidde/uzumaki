"""Inverse kinematics and top-down grasp planning for any single arm described by an ``ArmRig``.

An ``ArmRig`` holds everything robot-specific that the MuJoCo world needs: the robot description
(``uzumaki.robots.SingleArm``), its pinned model files, where its base stands on the tabletop,
its actuators, ready pose and gripper settings, the tool-centre site, the gripper pad bodies, and
how to seed inverse kinematics. ``uzumaki.sim.so101`` and ``uzumaki.sim.panda`` are rigs; the
kinematics, planning, scene and world code is shared.

A top-down grasp fixes the approach (fingers pointing down, the site's x axis) and the jaw yaw
(the site's z axis, along which the jaws close), plus the position. ``Kinematics.solve`` is damped
least squares from the rig's seeds, clamped to the joint ranges; with more than five joints the
solution is one of many and the seeds decide which. It returns ``None`` instead of a partial
solution.

Grasps leave the fixed jaw (an asymmetric gripper's static finger; for a symmetric gripper the
offset is zero and the box is centred) ``grasp_gap_m`` short of the box face. Of the four jaw yaws
that align with a square box, the planner keeps the reachable one with the smallest wrist rotation
that also admits a vertical place at the goal, if the goal is known and on the table. Planning
uses only the tool arguments (estimated pose) and the goal, never the true box pose.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import mujoco
import numpy as np

from uzumaki.fixture_env import TASK_PROTOCOL, FixtureCase, load_cases
from uzumaki.robots import SingleArm
from uzumaki.sim.assets import PinnedModel

POSITION_TOLERANCE_M = 1e-3
ROTATION_TOLERANCE = 5e-3
IK_ITERATIONS = 300
IK_DAMPING = 1e-5
ROTATION_WEIGHT = 0.1
#: Configurations checked on each straight joint-space segment of a plan.
PATH_SAMPLES = 12
#: Box bottom height above the tabletop at release (0: set the box down, then open fully).
PLACE_CLEARANCE_M = 0.0
HOVER_HEIGHTS_M = (0.05, 0.04, 0.03)
CARRY_HEIGHTS_M = (0.06, 0.045, 0.035)

#: ``seeds(bearing, roll)`` -> joint-space starting points; ``bearing`` is the target's direction
#: from the base relative to the base heading, ``roll`` the jaw yaw relative to that direction.
Seeds = Callable[[float, float], list[Any]]


@dataclass(frozen=True)
class ArmRig:
    name: str
    robot: SingleArm
    assets: PinnedModel
    #: The frozen development case set this arm can reach (``uzumaki.baseline-cases/1.0``).
    case_set: str
    cases_file: Path
    #: Root body of the robot in its model file; it is placed at ``base_xy`` facing ``base_yaw``.
    root_body: str
    base_xy: tuple[float, float]
    base_yaw_deg: float
    base_z_m: float
    arm_actuators: tuple[str, ...]
    gripper_actuator: str
    #: Joint whose position tells how far the gripper is open.
    gripper_joint: str
    grip_open: float
    grip_closed: float
    #: Holding: commanded below ``closed_below`` while the joint stays above ``opening_above``.
    closed_below: float
    opening_above: float
    ready: tuple[float, ...]
    #: Tool-centre site, x axis along the approach, z axis along the jaw closing direction.
    site: str
    #: When the model has no such site: the body it is added to, its position and quaternion.
    site_body: str | None
    site_pos: tuple[float, float, float]
    site_quat: tuple[float, float, float, float]
    #: Bodies whose geoms may touch the box (with ``pad_priority``, only geoms of that priority).
    pad_bodies: tuple[str, ...]
    pad_priority: int | None
    fixed_jaw_offset_m: float
    grasp_gap_m: float
    #: Lowest site height for a grasp or place, keeping the fingertips off the tabletop.
    min_site_z_m: float
    seeds: Seeds
    #: Index of the joint that turns the jaw (smaller rotation is preferred), and of the base pan.
    roll_index: int
    pan_index: int
    #: Cancel gravity on the robot's bodies, as a torque-controlled arm's controller does.
    gravity_compensation: bool = False
    #: Radius of a visual pedestal from the floor up to the base (0: the base stands on the table).
    pedestal_radius_m: float = 0.0
    #: Task protocol of ``cases_file``.
    protocol: str = TASK_PROTOCOL
    #: Overview camera for videos: look-at x, y, z (m), distance (m), azimuth, elevation (deg).
    camera: tuple[float, float, float, float, float, float] = (0.0, -0.05, 0.09, 0.85, -90.0, -32.0)

    @property
    def arm_joints(self) -> tuple[str, ...]:
        return tuple(self.robot.arm.joints)

    def cases(self) -> tuple[FixtureCase, ...]:
        return load_cases(self.cases_file, self.protocol)


#: Rig name -> ``module:attribute`` of its ``ArmRig`` (imported on demand).
RIGS: Mapping[str, str] = MappingProxyType(
    {"so101": "uzumaki.sim.so101:RIG", "panda": "uzumaki.sim.panda:RIG"}
)


def rig_named(name: str) -> ArmRig:
    ref = RIGS.get(name)
    if ref is None:
        raise KeyError(f"unknown arm {name!r}; known: {', '.join(sorted(RIGS))}")
    module, _, attribute = ref.partition(":")
    rig: ArmRig = getattr(importlib.import_module(module), attribute)
    return rig


def default_rig() -> ArmRig:
    """The SO-101 rig, used where no rig is given."""
    return rig_named("so101")


def wrap(angle: float) -> float:
    """Angle wrapped to [-pi, pi)."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def jaw_frame(jaw_yaw: float) -> Any:
    """Site orientation for a top-down grasp: x down, z (jaw axis) horizontal at ``jaw_yaw``."""
    x_axis = np.array([0.0, 0.0, -1.0])
    z_axis = np.array([math.cos(jaw_yaw), math.sin(jaw_yaw), 0.0])
    y_axis = np.cross(z_axis, x_axis)
    return np.stack([x_axis, y_axis, z_axis], axis=1)


def site_position(rig: ArmRig, center: Sequence[float], jaw_yaw: float, box_size: float) -> Any:
    """Site position that leaves the fixed jaw ``grasp_gap_m`` from the box face (a symmetric
    gripper has no fixed jaw: the site is the box centre)."""
    if rig.fixed_jaw_offset_m == 0.0:
        return np.asarray(center, dtype=float)
    axis = np.array([math.cos(jaw_yaw), math.sin(jaw_yaw), 0.0])
    offset = rig.fixed_jaw_offset_m - box_size / 2 - rig.grasp_gap_m
    return np.asarray(center, dtype=float) + offset * axis


def _descends(model: Any, body: int, ancestor: int) -> bool:
    while body != 0:
        if body == ancestor:
            return True
        body = int(model.body_parentid[body])
    return False


class Kinematics:
    """Forward and inverse kinematics on a private ``MjData`` (the simulation is untouched)."""

    def __init__(self, model: Any, rig: ArmRig) -> None:
        self.model = model
        self.rig = rig
        self.data = mujoco.MjData(model)
        self.site = model.site(rig.site).id
        joints = [model.joint(name).id for name in rig.arm_joints]
        self.qpos = np.array([model.jnt_qposadr[j] for j in joints])
        self.dofs = np.array([model.jnt_dofadr[j] for j in joints])
        self.low = np.array([model.jnt_range[j][0] for j in joints])
        self.high = np.array([model.jnt_range[j][1] for j in joints])
        self.gripper_qpos = model.jnt_qposadr[model.joint(rig.gripper_joint).id]
        self.finger_qpos = [
            model.jnt_qposadr[model.joint(name).id] for name in rig.robot.gripper.joints
        ]
        self.open_fingers = self._open_fingers()
        root = model.body(rig.root_body).id
        self.robot_geoms = {
            geom
            for geom in range(model.ngeom)
            if _descends(model, int(model.geom_bodyid[geom]), root)
        }
        self.obstacles = {model.geom(name).id for name in ("table", "floor")}
        base = model.body(rig.root_body)
        self.base_xy = (float(base.pos[0]), float(base.pos[1]))
        quat = base.quat
        self.base_yaw = math.atan2(
            2 * (quat[0] * quat[3] + quat[1] * quat[2]), 1 - 2 * (quat[2] ** 2 + quat[3] ** 2)
        )

    def _open_fingers(self) -> list[float]:
        """Finger joint positions with the gripper fully open (the widest it gets)."""
        model = self.model
        values = []
        for adr in self.finger_qpos:
            joint = next(j for j in range(model.njnt) if model.jnt_qposadr[j] == adr)
            low, high = model.jnt_range[joint]
            if adr == self.gripper_qpos:
                values.append(float(np.clip(self.rig.grip_open, low, high)))
            else:
                values.append(float(high))
        return values

    def _iterate(self, target: Any, rotation: Any, q: Any) -> tuple[Any, float, float]:
        model, data = self.model, self.data
        n = len(self.dofs)
        jac_p = np.zeros((3, model.nv))
        jac_r = np.zeros((3, model.nv))
        position_error = rotation_error = math.inf
        for _ in range(IK_ITERATIONS):
            data.qpos[self.qpos] = q
            mujoco.mj_kinematics(model, data)
            mujoco.mj_comPos(model, data)
            current = data.site_xmat[self.site].reshape(3, 3)
            dp = target - data.site_xpos[self.site]
            dr = 0.5 * sum(np.cross(current[:, i], rotation[:, i]) for i in range(3))
            position_error = float(np.linalg.norm(dp))
            rotation_error = float(np.linalg.norm(dr))
            if position_error < 2e-4 and rotation_error < 2e-3:
                break
            mujoco.mj_jacSite(model, data, jac_p, jac_r, self.site)
            jac = np.vstack([jac_p[:, self.dofs], ROTATION_WEIGHT * jac_r[:, self.dofs]])
            error = np.concatenate([dp, ROTATION_WEIGHT * dr])
            step = jac.T @ np.linalg.solve(jac @ jac.T + IK_DAMPING * np.eye(6), error)
            if n > 6:  # pull redundant joints gently toward the ready pose
                null = np.eye(n) - np.linalg.pinv(jac) @ jac
                step = step + 0.05 * null @ (np.asarray(self.rig.ready) - q)
            q = np.clip(q + step, self.low, self.high)
        return q, position_error, rotation_error

    def solve(self, position: Sequence[float], jaw_yaw: float, seed: Any = None) -> Any:
        """Joint positions for a top-down site pose, or ``None`` if none is within tolerance."""
        target = np.asarray(position, dtype=float)
        rotation = jaw_frame(jaw_yaw)
        bearing = math.atan2(target[1] - self.base_xy[1], target[0] - self.base_xy[0])
        seeds = [] if seed is None else [np.asarray(seed, dtype=float)]
        seeds += self.rig.seeds(wrap(bearing - self.base_yaw), wrap(jaw_yaw - bearing))
        for start in seeds:
            q, position_error, rotation_error = self._iterate(
                target, rotation, np.clip(start, self.low, self.high)
            )
            if position_error < POSITION_TOLERANCE_M and rotation_error < ROTATION_TOLERANCE:
                return q
        return None

    def collision_free(self, q: Any) -> bool:
        """No robot self contact and no robot contact with the table or floor at ``q`` (gripper
        open, the widest it gets)."""
        data = self.data
        data.qpos[self.qpos] = q
        for adr, value in zip(self.finger_qpos, self.open_fingers, strict=True):
            data.qpos[adr] = value
        mujoco.mj_kinematics(self.model, data)
        mujoco.mj_collision(self.model, data)
        for i in range(data.ncon):
            pair = {int(data.contact[i].geom1), int(data.contact[i].geom2)}
            robot = pair & self.robot_geoms
            if len(robot) == 2 or (robot and pair & self.obstacles):
                return False
        return True

    def path_clear(self, *poses: Any) -> bool:
        """Every sampled configuration on the joint-space segments between ``poses`` is
        collision free (the motion interpolates joints, so this is the path it follows)."""
        for start, end in zip(poses, poses[1:], strict=False):
            for k in range(PATH_SAMPLES + 1):
                q = np.asarray(start) + (np.asarray(end) - np.asarray(start)) * k / PATH_SAMPLES
                if not self.collision_free(q):
                    return False
        return True

    def first(
        self, position: Sequence[float], jaw_yaw: float, heights: Sequence[float], seed: Any
    ) -> Any:
        """The first reachable pose at ``position`` raised by one of ``heights``."""
        for height in heights:
            raised = np.asarray(position, dtype=float) + np.array([0.0, 0.0, height])
            q = self.solve(raised, jaw_yaw, seed)
            if q is not None:
                return q
        return None

    def ready_toward(self, pose: Any) -> Any:
        """The ready pose with the pan and wrist rotation of ``pose``: where an approach starts."""
        ready = np.array(self.rig.ready)
        ready[self.rig.pan_index] = pose[self.rig.pan_index]
        ready[self.rig.roll_index] = pose[self.rig.roll_index]
        return ready


@dataclass(frozen=True)
class Grasp:
    """A planned top-down grasp: jaw yaw, the estimated box yaw and size, and joint poses."""

    jaw_yaw: float
    box_yaw: float
    box_size: float
    hover: Any
    grasp: Any
    carry: Any


@dataclass(frozen=True)
class Placement:
    """A planned vertical place: approach pose above the target and the release pose."""

    approach: Any
    release: Any


def plan_place(
    kin: Kinematics, grasp: Grasp, x: float, y: float, z: float, yaw_deg: float
) -> Placement | None:
    """Release pose turning the box from its estimated yaw at the pick to ``yaw_deg``."""
    rig = kin.rig
    jaw_yaw = wrap(grasp.jaw_yaw + math.radians(yaw_deg) - grasp.box_yaw)
    height = max(z, rig.min_site_z_m) + PLACE_CLEARANCE_M
    release_site = site_position(rig, (x, y, height), jaw_yaw, grasp.box_size)
    approach = kin.first(release_site, jaw_yaw, CARRY_HEIGHTS_M, None)
    if approach is None:
        return None
    release = kin.solve(release_site, jaw_yaw, approach)
    if release is None or not kin.path_clear(grasp.carry, approach, release):
        return None
    return Placement(approach, release)


def _turn(rig: ArmRig, q: Any) -> float:
    """How far the wrist rotation joint is from its ready position."""
    return abs(float(q[rig.roll_index]) - rig.ready[rig.roll_index])


def plan_grasp(
    kin: Kinematics,
    x: float,
    y: float,
    yaw_deg: float,
    grasp_height: float,
    goal: tuple[float, float, float, float] | None,
    quarters: Sequence[int] = (0, 1, 2, 3),
) -> Grasp | None:
    """Plan a grasp of a square box of size ``2 * grasp_height`` at the estimated pose; the jaw
    yaw is the box yaw plus one of ``quarters`` quarter turns (``(1, 3)``: across a bar)."""
    rig = kin.rig
    box_yaw = math.radians(yaw_deg)
    size = 2 * grasp_height
    best: Grasp | None = None
    fallback: Grasp | None = None
    for quarter in quarters:
        jaw_yaw = wrap(box_yaw + quarter * math.pi / 2)
        site = site_position(rig, (x, y, max(grasp_height, rig.min_site_z_m)), jaw_yaw, size)
        grasp_q = kin.solve(site, jaw_yaw)
        if grasp_q is None:
            continue
        hover = kin.first(site, jaw_yaw, HOVER_HEIGHTS_M, grasp_q)
        carry = kin.first(site, jaw_yaw, CARRY_HEIGHTS_M, grasp_q)
        if hover is None or carry is None:
            continue
        if not kin.path_clear(kin.ready_toward(hover), hover, grasp_q, carry):
            continue
        candidate = Grasp(jaw_yaw, box_yaw, size, hover, grasp_q, carry)
        if fallback is None or _turn(rig, grasp_q) < _turn(rig, fallback.grasp):
            fallback = candidate
        if goal is not None and plan_place(kin, candidate, *goal) is None:
            continue
        if best is None or _turn(rig, grasp_q) < _turn(rig, best.grasp):
            best = candidate
    return best if best is not None else fallback
