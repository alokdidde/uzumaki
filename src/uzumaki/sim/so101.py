"""Inverse kinematics and grasp planning for top-down SO-101 grasps.

The SO-101 arm has five joints (pan, lift, elbow, wrist flex, wrist roll) and a one-joint gripper.
A top-down grasp fixes the approach (fingers pointing down, the ``gripperframe`` site's x axis)
and the jaw yaw (the site's z axis, along which the moving jaw closes onto the fixed jaw), which
together with the position is exactly what five joints can reach. ``Kinematics.solve`` is damped
least squares from a few seeds, clamped to the joint ranges; it returns ``None`` instead of a
partial solution.

Grasps are planned so the fixed jaw stops ``GRASP_GAP_M`` short of the box face and the moving jaw
pushes the box onto it. Of the four jaw yaws that align with a square box, the planner keeps the
reachable one with the smallest wrist roll that also admits a vertical place at the goal, if the
goal is known and on the table. Planning uses only the tool arguments (estimated pose) and the
goal, never the true box pose.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import mujoco
import numpy as np

ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
GRIPPER_JOINT = "gripper"
SITE = "gripperframe"
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
#: Box bottom height above the tabletop at release (0: set the box down, then open fully).
PLACE_CLEARANCE_M = 0.0
HOVER_HEIGHTS_M = (0.05, 0.04, 0.03)
CARRY_HEIGHTS_M = (0.06, 0.045, 0.035)
POSITION_TOLERANCE_M = 1e-3
ROTATION_TOLERANCE = 5e-3
IK_ITERATIONS = 300
IK_DAMPING = 1e-5
ROTATION_WEIGHT = 0.1
#: Configurations checked on each straight joint-space segment of a plan.
PATH_SAMPLES = 12


def _descends(model: Any, body: int, ancestor: int) -> bool:
    while body != 0:
        if body == ancestor:
            return True
        body = int(model.body_parentid[body])
    return False


def ready_toward(pose: Any) -> Any:
    """``READY`` with the pan and wrist roll of ``pose``: where an approach starts from."""
    ready = np.array(READY)
    ready[0], ready[4] = pose[0], pose[4]
    return ready


def wrap(angle: float) -> float:
    """Angle wrapped to [-pi, pi)."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def jaw_frame(jaw_yaw: float) -> Any:
    """Site orientation for a top-down grasp: x down, z (jaw axis) horizontal at ``jaw_yaw``."""
    x_axis = np.array([0.0, 0.0, -1.0])
    z_axis = np.array([math.cos(jaw_yaw), math.sin(jaw_yaw), 0.0])
    y_axis = np.cross(z_axis, x_axis)
    return np.stack([x_axis, y_axis, z_axis], axis=1)


def site_position(center: Sequence[float], jaw_yaw: float, box_size: float) -> Any:
    """Site position that leaves the fixed jaw ``GRASP_GAP_M`` from the box face."""
    axis = np.array([math.cos(jaw_yaw), math.sin(jaw_yaw), 0.0])
    offset = FIXED_JAW_OFFSET_M - box_size / 2 - GRASP_GAP_M
    return np.asarray(center, dtype=float) + offset * axis


class Kinematics:
    """Forward and inverse kinematics on a private ``MjData`` (the simulation is untouched)."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.data = mujoco.MjData(model)
        self.site = model.site(SITE).id
        joints = [model.joint(name).id for name in ARM_JOINTS]
        self.qpos = np.array([model.jnt_qposadr[j] for j in joints])
        self.dofs = np.array([model.jnt_dofadr[j] for j in joints])
        self.low = np.array([model.jnt_range[j][0] for j in joints])
        self.high = np.array([model.jnt_range[j][1] for j in joints])
        self.gripper_qpos = model.jnt_qposadr[model.joint(GRIPPER_JOINT).id]
        base_id = model.body("base").id
        self.robot_geoms = {
            geom
            for geom in range(model.ngeom)
            if _descends(model, int(model.geom_bodyid[geom]), base_id)
        }
        self.obstacles = {model.geom(name).id for name in ("table", "floor")}
        base = model.body("base")
        self.base_xy = (float(base.pos[0]), float(base.pos[1]))
        quat = base.quat
        self.base_yaw = math.atan2(
            2 * (quat[0] * quat[3] + quat[1] * quat[2]), 1 - 2 * (quat[2] ** 2 + quat[3] ** 2)
        )

    def _iterate(self, target: Any, rotation: Any, q: Any) -> tuple[Any, float, float]:
        model, data = self.model, self.data
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
            q = np.clip(q + step, self.low, self.high)
        return q, position_error, rotation_error

    def solve(self, position: Sequence[float], jaw_yaw: float, seed: Any = None) -> Any:
        """Joint positions for a top-down site pose, or ``None`` if none is within tolerance."""
        target = np.asarray(position, dtype=float)
        rotation = jaw_frame(jaw_yaw)
        bearing = math.atan2(target[1] - self.base_xy[1], target[0] - self.base_xy[0])
        pan = -wrap(bearing - self.base_yaw)
        roll = wrap(jaw_yaw - bearing)
        seeds = [] if seed is None else [np.asarray(seed, dtype=float)]
        seeds += [
            np.array([pan, lift, elbow, flex, roll])
            for lift, elbow, flex in ((0.0, 0.3, 1.2), (-0.5, 1.0, 1.2), (0.5, -0.3, 1.4))
        ]
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
        data.qpos[self.gripper_qpos] = GRIP_OPEN
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


def _site_z(height: float) -> float:
    return max(height, MIN_SITE_Z_M)


def plan_place(
    kin: Kinematics, grasp: Grasp, x: float, y: float, z: float, yaw_deg: float
) -> Placement | None:
    """Release pose turning the box from its estimated yaw at the pick to ``yaw_deg``."""
    jaw_yaw = wrap(grasp.jaw_yaw + math.radians(yaw_deg) - grasp.box_yaw)
    release_site = site_position((x, y, _site_z(z) + PLACE_CLEARANCE_M), jaw_yaw, grasp.box_size)
    approach = kin.first(release_site, jaw_yaw, CARRY_HEIGHTS_M, None)
    if approach is None:
        return None
    release = kin.solve(release_site, jaw_yaw, approach)
    if release is None or not kin.path_clear(grasp.carry, approach, release):
        return None
    return Placement(approach, release)


def plan_grasp(
    kin: Kinematics,
    x: float,
    y: float,
    yaw_deg: float,
    grasp_height: float,
    goal: tuple[float, float, float, float] | None,
) -> Grasp | None:
    """Plan a grasp of a square box of size ``2 * grasp_height`` at the estimated pose."""
    box_yaw = math.radians(yaw_deg)
    size = 2 * grasp_height
    best: Grasp | None = None
    fallback: Grasp | None = None
    for quarter in range(4):
        jaw_yaw = wrap(box_yaw + quarter * math.pi / 2)
        site = site_position((x, y, _site_z(grasp_height)), jaw_yaw, size)
        grasp_q = kin.solve(site, jaw_yaw)
        if grasp_q is None:
            continue
        hover = kin.first(site, jaw_yaw, HOVER_HEIGHTS_M, grasp_q)
        carry = kin.first(site, jaw_yaw, CARRY_HEIGHTS_M, grasp_q)
        if hover is None or carry is None:
            continue
        if not kin.path_clear(ready_toward(hover), hover, grasp_q, carry):
            continue
        candidate = Grasp(jaw_yaw, box_yaw, size, hover, grasp_q, carry)
        if fallback is None or abs(grasp_q[4]) < abs(fallback.grasp[4]):
            fallback = candidate
        if goal is not None and plan_place(kin, candidate, *goal) is None:
            continue
        if best is None or abs(grasp_q[4]) < abs(best.grasp[4]):
            best = candidate
    return best if best is not None else fallback
