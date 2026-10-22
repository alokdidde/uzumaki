"""MuJoCo physics world behind the four ``pick-place-v1`` tools, and its independent evaluator.

``SimWorld`` replaces the scripted fixture world with physics while keeping its contract: the same
admission checks in the same order (inherited from ``FixtureWorld``), the same admission log and
audit, the same observation fields and the same shared integer-millisecond clock, which advances
by exactly one 5 ms physics step at a time. ``observe`` holds the arm for 100 ms. ``pick`` and
``place`` are closed motion sequences planned by ``uzumaki.sim.so101`` from the tool arguments
(estimated pose) and the goal; their duration is whatever the motion takes. Whether a pick holds
the box is read from the gripper joint, as a real servo would report it.

The evaluator is privileged: it reads the true box pose, velocity and contacts from the physics
state. Completion means the box is released (no pad contact), resting on the tabletop, within
0.02 m and 10 degrees of the target and below 0.01 m/s for at least 0.5 s before the deadline.
Forbidden contacts are recorded on every physics step and end the case as ``collision``: any
robot contact with the tabletop or floor, robot self contact, robot/box contact other than the
gripper pads, and box contact with the floor or with the table outside its x/y bounds. A reset
that does not leave the box resting without forbidden contacts is ``evaluation_failure``.

Injected fixture faults keep their meaning: ``misgrasp_picks`` makes those picks close the
gripper at the hover height (a real physical miss); stale observes, worker crashes and goal
withdrawal are applied exactly as in the fixture.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from typing import Any

import mujoco
import numpy as np

from uzumaki.fixture_env import (
    ESTIMATE_OFFSET,
    SETTLE_WINDOW_MS,
    STALE_LAG_MS,
    TABLE_BOUND_M,
    WALL_MS,
    XY_TOLERANCE_M,
    YAW_TOLERANCE_DEG,
    FixtureCase,
    FixtureClock,
    FixtureEvaluator,
    FixtureWorld,
)
from uzumaki.sim import so101
from uzumaki.sim.scene import build_model

STEP_MS = 5
OBSERVE_MS = 100
ABORT_MS = 100
RESET_SETTLE_S = 0.3
SPEED_LIMIT_M_S = 0.01
#: Box displacement (m) or yaw change (degrees) that counts as moving it.
MOVED_M = 0.002
MOVED_DEG = 1.0
#: Box height above its resting height (m) that counts as lifted.
LIFTED_M = 0.01
RESTING_TOLERANCE_M = 0.003
MAX_CONTACT_EVENTS = 32
#: Motion durations in seconds.
APPROACH_S = 0.8
DESCEND_S = 0.8
CLOSE_S = 0.5
GRIP_SETTLE_S = 0.3
LIFT_S = 0.8
CARRY_S = 1.8
RELEASE_S = 0.4
RELEASE_SETTLE_S = 0.2
RETREAT_S = 0.6

FrameHook = Callable[[Any, Any], None]


def _yaw_deg(quat: Any) -> float:
    w, x, y, z = (float(v) for v in quat)
    return math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


def _wrapped_deg(angle: float) -> float:
    return abs((angle + 180.0) % 360.0 - 180.0)


def _smoothstep(a: float) -> float:
    return a * a * (3 - 2 * a)


class SimWorld(FixtureWorld):
    """Physics state for one case and the host handlers of the four declared tools."""

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
    ) -> None:
        super().__init__(case, clock)
        if abs(model.opt.timestep * 1000 - STEP_MS) > 1e-9:
            raise ValueError("simulation timestep must be 5 ms")
        self.model = model
        self.data = mujoco.MjData(model)
        self.kin = so101.Kinematics(model)
        self.frame_hook = frame_hook
        self.grasp: so101.Grasp | None = None
        self.contact_events: list[dict[str, Any]] = []
        self.settled_since_ms: int | None = None
        self.ready = False
        self._actuators = np.array(
            [model.actuator(name).id for name in (*so101.ARM_JOINTS, so101.GRIPPER_JOINT)]
        )
        self._gripper_qpos = model.jnt_qposadr[model.joint(so101.GRIPPER_JOINT).id]
        box_joint = model.body("box").jntadr[0]
        self._box_qpos = model.jnt_qposadr[box_joint]
        self._box_dof = model.jnt_dofadr[box_joint]
        self._classify_geoms()
        self._reset()

    # --------------------------------------------------------------------------- physics
    def _classify_geoms(self) -> None:
        model = self.model
        base = model.body("base").id
        robot_bodies = {b for b in range(model.nbody) if self._descends(b, base)}
        pad_bodies = {model.body("gripper").id, model.body("moving_jaw_so101_v1").id}
        self._names: dict[int, str] = {}
        self._robot: set[int] = set()
        self._pads: set[int] = set()
        for geom in range(model.ngeom):
            body = int(model.geom_bodyid[geom])
            self._names[geom] = model.geom(geom).name or f"{model.body(body).name}:{geom}"
            if body in robot_bodies:
                self._robot.add(geom)
                if body in pad_bodies and int(model.geom_priority[geom]) == 1:
                    self._pads.add(geom)
        self._box = model.geom("box").id
        self._table = model.geom("table").id
        self._floor = model.geom("floor").id

    def _descends(self, body: int, ancestor: int) -> bool:
        while body != 0:
            if body == ancestor:
                return True
            body = int(self.model.body_parentid[body])
        return False

    def _forbidden(self, a: int, b: int) -> bool:
        pair = {a, b}
        robot = pair & self._robot
        if len(robot) == 2:
            return True  # self contact (MuJoCo already excludes parent/child pairs)
        if robot:
            other = (pair - robot).pop()
            return other != self._box or not robot <= self._pads
        if self._box in pair and self._floor in pair:
            return True
        if self._box in pair and self._table in pair:
            x, y = self.box_xy()
            return abs(x) > TABLE_BOUND_M or abs(y) > TABLE_BOUND_M
        return False

    def _check_contacts(self) -> bool:
        """Record forbidden contacts; return whether the robot touches the box."""
        touching = False
        for i in range(self.data.ncon):
            contact = self.data.contact[i]
            a, b = int(contact.geom1), int(contact.geom2)
            if self._box in (a, b) and ({a, b} & self._robot):
                touching = True
            if self._forbidden(a, b):
                pair = sorted((self._names[a], self._names[b]))
                known = any(event["pair"] == pair for event in self.contact_events)
                if not known and len(self.contact_events) < MAX_CONTACT_EVENTS:
                    self.contact_events.append({"at_ms": self.clock(), "pair": pair})
        return touching

    def _step(self, count: int) -> None:
        for _ in range(count):
            mujoco.mj_step(self.model, self.data)
            self.clock.advance(STEP_MS)
            touching = self._check_contacts()
            if self.released_ms is not None:
                if touching or self.box_speed() > SPEED_LIMIT_M_S:
                    self.settled_since_ms = None
                elif self.settled_since_ms is None:
                    self.settled_since_ms = self.clock()
            if self.box_z() > self.case.box_size_m / 2 + LIFTED_M:
                self.lifted = True
            if self.frame_hook is not None:
                self.frame_hook(self.model, self.data)

    def _steps(self, seconds: float) -> int:
        return max(1, round(seconds * 1000 / STEP_MS))

    def _move(self, target: Any, seconds: float, grip: float | None = None) -> None:
        """Smoothstep joint-space motion of the position actuators to ``target``."""
        start = self.data.ctrl[self._actuators[:5]].copy()
        grip_start = float(self.data.ctrl[self._actuators[5]])
        count = self._steps(seconds)
        for k in range(count):
            a = _smoothstep((k + 1) / count)
            self.data.ctrl[self._actuators[:5]] = start + (np.asarray(target) - start) * a
            if grip is not None:
                self.data.ctrl[self._actuators[5]] = grip_start + (grip - grip_start) * a
            self._step(1)

    def _hold(self, seconds: float) -> None:
        self._step(self._steps(seconds))

    def _reset(self) -> None:
        """Ready pose, open gripper, box settled on the tabletop; the case clock does not run."""
        ready = np.array(so101.READY)
        self.data.qpos[self.kin.qpos] = ready
        self.data.ctrl[self._actuators[:5]] = ready
        self.data.qpos[self._gripper_qpos] = so101.GRIP_OPEN
        self.data.ctrl[self._actuators[5]] = so101.GRIP_OPEN
        mujoco.mj_forward(self.model, self.data)
        clean = True
        for _ in range(round(RESET_SETTLE_S * 1000 / STEP_MS)):
            mujoco.mj_step(self.model, self.data)
            for i in range(self.data.ncon):
                contact = self.data.contact[i]
                clean = clean and not self._forbidden(int(contact.geom1), int(contact.geom2))
        resting = abs(self.box_z() - self.case.box_size_m / 2) <= RESTING_TOLERANCE_M
        self.ready = clean and resting and self.box_speed() <= SPEED_LIMIT_M_S
        self._initial_pose = self.box_pose()
        if self.frame_hook is not None:
            self.frame_hook(self.model, self.data)

    # ------------------------------------------------------------------------ true state
    def box_xy(self) -> tuple[float, float]:
        position = self.data.qpos[self._box_qpos : self._box_qpos + 2]
        return float(position[0]), float(position[1])

    def box_z(self) -> float:
        return float(self.data.qpos[self._box_qpos + 2])

    def box_yaw_deg(self) -> float:
        return _yaw_deg(self.data.qpos[self._box_qpos + 3 : self._box_qpos + 7])

    def box_speed(self) -> float:
        return float(np.linalg.norm(self.data.qvel[self._box_dof : self._box_dof + 3]))

    def box_pose(self) -> tuple[float, float, float]:
        x, y = self.box_xy()
        return x, y, self.box_yaw_deg()

    def _displaced(self, before: tuple[float, float, float]) -> bool:
        x, y, yaw = self.box_pose()
        return (
            math.hypot(x - before[0], y - before[1]) > MOVED_M
            or _wrapped_deg(yaw - before[2]) > MOVED_DEG
        )

    def _gripper_holding(self) -> bool:
        closed = float(self.data.ctrl[self._actuators[5]]) < so101.HOLDING_THRESHOLD
        return closed and float(self.data.qpos[self._gripper_qpos]) > so101.HOLDING_THRESHOLD

    def _goal(self) -> tuple[float, float, float, float] | None:
        x, y = self.case.target_xy_m
        if abs(x) > TABLE_BOUND_M or abs(y) > TABLE_BOUND_M:
            return None
        return x, y, self.case.box_size_m / 2, 0.0

    # ------------------------------------------------------------------------------ tools
    def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._reply("rejected", reason="malformed_arguments")
        self._hold(OBSERVE_MS / 1000)
        self.held = self.held and self._gripper_holding()
        self.observes += 1
        now = self.clock()
        stamp = now - STALE_LAG_MS if self.observes in self.case.stale_observes else now
        observation_id = f"obs-{self.observes}"
        self._issued[observation_id] = stamp
        self._latest = observation_id
        dx, dy, dyaw = ESTIMATE_OFFSET
        x, y, yaw = self.box_pose()
        target_x, target_y = self.case.target_xy_m
        return self._reply(
            "ok",
            observation_id=observation_id,
            timestamp_ms=stamp,
            object_pose_estimate={
                "x_m": round(x + dx, 6),
                "y_m": round(y + dy, 6),
                "yaw_deg": round(yaw + dyaw, 6),
            },
            target_pose={
                "x_m": target_x,
                "y_m": target_y,
                "z_m": round(self.case.box_size_m / 2, 6),
                "yaw_deg": 0.0,
            },
            gripper_state="holding" if self.held else "open",
            tool_result=self._last_result,
        )

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "yaw_deg", "grasp_height_m"))
        if reason is None and not all(abs(v) <= TABLE_BOUND_M for v in self.box_xy()):
            reason = "unreachable_target"
        if reason is None and self.held:
            reason = "already_held"
        grasp: so101.Grasp | None = None
        if reason is None:
            grasp = so101.plan_grasp(
                self.kin,
                args["x_m"],
                args["y_m"],
                args["yaw_deg"],
                args["grasp_height_m"],
                self._goal(),
            )
            if grasp is None:
                reason = "unreachable_target"
        entry = self._log("pick", reason, context)
        if reason is not None or grasp is None:
            return self._reply("rejected", reason=reason)
        self.picks += 1
        before = self.box_pose()
        fumble = self.picks <= self.case.misgrasp_picks
        self._move(so101.ready_toward(grasp.hover), APPROACH_S, grip=so101.GRIP_OPEN)
        self._move(grasp.hover, APPROACH_S)
        closing = grasp.hover if fumble else grasp.grasp
        if not fumble:
            self._move(grasp.grasp, DESCEND_S)
        self._move(closing, CLOSE_S, grip=so101.GRIP_CLOSED)
        self._hold(GRIP_SETTLE_S)
        self._move(grasp.carry, LIFT_S)
        entry["effect"] = self.lifted or self._displaced(before)
        if self._gripper_holding():
            self.held = True
            self.grasp = grasp
            self._last_result = "held"
            return self._reply("ok", held=True)
        self._last_result = "misgrasp"
        return self._reply("error", held=False, reason="misgrasp")

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
        if reason is None and not (
            abs(args["x_m"]) <= TABLE_BOUND_M and abs(args["y_m"]) <= TABLE_BOUND_M
        ):
            reason = "unreachable_target"
        if reason is None and (not self.held or self.grasp is None):
            reason = "not_held"
        placement: so101.Placement | None = None
        if reason is None and self.grasp is not None:
            placement = so101.plan_place(
                self.kin, self.grasp, args["x_m"], args["y_m"], args["z_m"], args["yaw_deg"]
            )
            if placement is None:
                reason = "unreachable_target"
        entry = self._log("place", reason, context)
        if reason is not None or placement is None:
            return self._reply("rejected", reason=reason)
        before = self.box_pose()
        self._move(placement.approach, CARRY_S)
        self._move(placement.release, DESCEND_S)
        self._move(placement.release, RELEASE_S, grip=1.2)
        self.held = False
        self.grasp = None
        self.released_ms = self.clock()
        self._hold(RELEASE_SETTLE_S)
        self._move(placement.approach, RETREAT_S)
        entry["effect"] = self._displaced(before)
        self._last_result = "released"
        return self._reply("ok", released=True)

    def abort(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._reply("rejected", reason="malformed_arguments")
        self.data.ctrl[self._actuators[:5]] = self.data.qpos[self.kin.qpos]
        self._hold(ABORT_MS / 1000)
        self.aborted = True
        self._log("abort", None, {"observed": self._latest is not None, "fresh": True})
        return self._reply("ok", held=self.held)

    # ------------------------------------------------------------------------- privileged
    def moved(self) -> bool:
        return self.lifted or self._displaced(self._initial_pose)

    def completion_holds(self, now: int) -> bool:
        if self.held or self.released_ms is None or not self.goal_active:
            return False
        if self.settled_since_ms is None or now - self.settled_since_ms < SETTLE_WINDOW_MS:
            return False
        if abs(self.box_z() - self.case.box_size_m / 2) > RESTING_TOLERANCE_M:
            return False
        x, y, yaw = self.box_pose()
        target_x, target_y = self.case.target_xy_m
        return (
            math.hypot(x - target_x, y - target_y) <= XY_TOLERANCE_M
            and _wrapped_deg(yaw) <= YAW_TOLERANCE_DEG
            and now < self.started_ms + WALL_MS
        )

    def audit(self) -> dict[str, Any]:
        audit = super().audit()
        audit["violations"] += len(self.contact_events)
        audit["contacts"] = copy.deepcopy(self.contact_events)
        return audit

    def final_state(self) -> dict[str, Any]:
        """True final box pose and errors, for reports (never shown to skills or selectors)."""
        x, y, yaw = self.box_pose()
        target_x, target_y = self.case.target_xy_m
        return {
            "box_xy_m": [round(x, 4), round(y, 4)],
            "box_yaw_deg": round(yaw, 2),
            "xy_error_m": round(math.hypot(x - target_x, y - target_y), 4),
            "yaw_error_deg": round(_wrapped_deg(yaw), 2),
            "sim_ms": self.clock() - self.started_ms,
        }


class SimEvaluator(FixtureEvaluator):
    """Fixture evaluator semantics plus physics: forbidden contacts are ``collision`` and a failed
    reset is ``evaluation_failure``; both precede completion."""

    def __init__(self, world: SimWorld) -> None:
        super().__init__(world)
        self.sim = world

    def _status(self) -> dict[str, Any]:
        if self.terminal is None:
            if self.sim.contact_events:
                self.terminal = "collision"
            elif not self.sim.ready:
                self.terminal = "evaluation_failure"
        return super()._status()

    def _final(self, arguments: Any) -> dict[str, Any]:
        verdict = super()._final(arguments)
        assert self.verdict is not None
        self.verdict["invariant_evidence"] = "sim_admission_log_and_contacts"
        verdict["invariant_evidence"] = self.verdict["invariant_evidence"]
        return verdict


def new_world(
    case: FixtureCase, clock: FixtureClock, assets: Any, frame_hook: FrameHook | None = None
) -> SimWorld:
    """Build the scene for ``case`` from verified assets and wrap it in a ``SimWorld``."""
    return SimWorld(case, clock, build_model(case, assets), frame_hook)
