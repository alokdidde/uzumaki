"""Stanford TidyBot in MuJoCo: a mobile manipulator that fetches a box from one table to another
(``fetch-v1``).

The room is the ``uzumaki.fetch_env`` layout: two 0.5 x 0.5 m tables ``TABLE_HEIGHT_M`` high with
tops at z = 0, table ``a`` at the origin and table ``b`` 0.8 m along x, each worked from its -y
edge. The robot is the ``MobileManipulator`` description ``uzumaki.embodiments.TIDYBOT``: a
holonomic base (position-controlled x, y and turn joints, as the Menagerie model has them), a
Kinova Gen3 arm on the shared arm engine (``RIG``, an ``ArmRig`` with a ``tcp`` site added between
the Robotiq 2F-85 pads) and the gripper. Kinematics follow the base: before planning, the arm
model is moved to the base pose the base actually holds.

``TidybotWorld`` implements the five ``fetch-v1`` tools. ``go_to`` drives the base to the work
station in front of the table under the given point (``fetch_env.station``: base centre 0.27 m
from the table edge, facing +y), with the arm raised for travel; a point on no table is
``unreachable_target``, and so is a station the base cannot reach on a straight collision-free
path. ``pick`` and ``place`` are the shared arm engine's top-down grasp and vertical place, and are
admitted only for a pose within reach of where the base stands: on a table, 0.25 to 0.68 m from
the base centre and within 20 degrees of its heading (top-down grasps were measured to reach 0.20
to 0.70 m at every jaw yaw). Whether the gripper holds the box is read from its driver joint. The
case fault ``stopped_short_drives`` stops that many drives 40 % of the way, which leaves the
object out of reach. The evaluator is ``SimEvaluator`` unchanged: any robot contact with a table
or the floor is ``collision``.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from uzumaki.baseline import CaseEnvironment
from uzumaki.embodiments import TIDYBOT
from uzumaki.fetch_env import START_POSE, TABLES, station, table_under
from uzumaki.fixture_env import (
    ACTUATING_TOOLS,
    ESTIMATE_OFFSET,
    FETCH_PROTOCOL,
    FETCH_TOOLS,
    TABLE_BOUND_M,
    FixtureCase,
    FixtureClock,
    load_cases,
)
from uzumaki.robots import SingleArm
from uzumaki.sim import arm
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import TIDYBOT_MODEL
from uzumaki.sim.scene import BAR_FRICTION, BOX_DENSITY, BOX_DROP_M, TIMESTEP_S, _mount, _yaw_quat
from uzumaki.sim.world import (
    APPROACH_S,
    DESCEND_S,
    RELEASE_S,
    RESTING_TOLERANCE_M,
    RETREAT_S,
    FrameHook,
    SimEvaluator,
    SimWorld,
    _smoothstep,
)

CASES_FILE = Path(__file__).parent / "data" / "tidybot_cases_v1.json"
CASE_SET = "tidybot-dev-v1"
TABLE_HEIGHT_M = 0.45
FLOOR_Z_M = -TABLE_HEIGHT_M
#: The model's ``home`` keyframe: the gripper level, pointing ahead of the base.
READY = (0.0, 0.262, 3.1416, -2.269, 0.0, 0.960, 1.571)
#: A pose is within reach when it is on a table, this far from the base centre (m) and within
#: ``REACH_BEARING_DEG`` of the base heading.
REACH_M = (0.25, 0.68)
REACH_BEARING_DEG = 20.0
#: Base speed limits while driving (m/s, rad/s) and the fraction of a drive a stopped-short
#: fault completes.
DRIVE_SPEED_M_S = 0.4
TURN_SPEED_RAD_S = 1.0
STOP_SHORT_FRACTION = 0.4
#: Box lift above the tabletop for travel (m), and base positions checked per metre of a drive.
TRAVEL_LIFT_M = 0.1
#: Seconds to carry the box from the travel pose to over the target: slowly, because the
#: 2F-85 model holds a small box with little force and a faster swing lets it slip.
PLACE_CARRY_S = 4.0
PATH_SAMPLES_PER_M = 20
#: Driver joint position (rad) below which a closed 2F-85 is holding something: empty, it closes
#: to its 0.8 rad limit.
HOLDING_BELOW_RAD = 0.75
BASE_JOINTS = ("joint_x", "joint_y", "joint_th")
GRIPPER_LINKS = ("driver", "coupler", "spring_link", "follower", "pad")


def _seeds(bearing: float, roll: float) -> list[Any]:
    # Elbow-up top-down configurations, joint_1 toward the target; joint_7 turns the jaws.
    wrist = arm.wrap(math.pi / 2 - roll)
    out = []
    for j2, j4, j6 in ((0.5, -1.6, -1.1), (0.8, -1.2, -1.0), (0.3, -2.0, -1.0), (1.0, -0.8, -0.9)):
        out.append(np.array([bearing, j2, math.pi, j4, 0.0, j6, wrist]))
        out.append(np.array([bearing, -j2, 0.0, j4, 0.0, -j6, wrist]))
    return out


RIG = ArmRig(
    name="tidybot",
    robot=SingleArm("tidybot_arm", model=TIDYBOT.model, arm=TIDYBOT.arm, gripper=TIDYBOT.gripper),
    assets=TIDYBOT_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="base_link",
    base_xy=(0.0, 0.0),
    base_yaw_deg=0.0,
    base_z_m=FLOOR_Z_M,
    arm_actuators=tuple(f"joint_{i}" for i in range(1, 8)),
    gripper_actuator="fingers_actuator",
    gripper_joint="right_driver_joint",
    grip_open=0.0,
    grip_closed=255.0,
    closed_below=128.0,
    opening_above=0.0,
    ready=READY,
    site="tcp",
    site_body="bracelet_link",
    site_pos=(0.0, 0.0, -0.21),
    site_quat=(math.sqrt(0.5), 0.0, math.sqrt(0.5), 0.0),
    pad_bodies=("left_pad", "right_pad", "left_follower", "right_follower"),
    pad_priority=None,
    fixed_jaw_offset_m=0.0,
    grasp_gap_m=0.0,
    min_site_z_m=0.018,
    seeds=_seeds,
    roll_index=6,
    pan_index=0,
    gravity_compensation=True,
    protocol=FETCH_PROTOCOL,
    camera=(0.4, 0.0, -0.05, 2.5, 50.0, -30.0),
)


def build_model(case: FixtureCase, assets: Path) -> Any:
    """Compile the room for ``case``: the robot, two tables, the box on table ``a`` and a
    visual-only target marker."""
    spec = mujoco.MjSpec.from_file(str(assets / RIG.assets.entry))
    spec.option.timestep = TIMESTEP_S
    spec.visual.global_.offwidth = 1280
    spec.visual.global_.offheight = 960
    spec.visual.headlight.ambient = [0.35, 0.35, 0.35]
    spec.visual.headlight.diffuse = [0.55, 0.55, 0.55]
    for key in list(spec.keys):
        spec.delete(key)
    _mount(spec, RIG)
    # The 2F-85's linkage bodies on each side touch each other as it moves; that is how the
    # mechanism is built, not a collision (the model already excludes some of these pairs).
    for side in ("left", "right"):
        links = ["base", *(f"{side}_{link}" for link in GRIPPER_LINKS)]
        for i, first in enumerate(links):
            for second in links[i + 1 :]:
                exclude = spec.add_exclude()
                exclude.bodyname1 = first
                exclude.bodyname2 = second
    world = spec.worldbody

    floor = world.add_geom()
    floor.name = "floor"
    floor.type = mujoco.mjtGeom.mjGEOM_PLANE
    floor.size = [3.0, 3.0, 0.05]
    floor.pos = [0.4, 0.0, FLOOR_Z_M]
    floor.rgba = [0.18, 0.2, 0.24, 1.0]
    for name, (cx, cy) in TABLES.items():
        table = world.add_geom()
        table.name = "table" if name == "a" else f"table_{name}"
        table.type = mujoco.mjtGeom.mjGEOM_BOX
        table.size = [TABLE_BOUND_M, TABLE_BOUND_M, TABLE_HEIGHT_M / 2]
        table.pos = [cx, cy, -TABLE_HEIGHT_M / 2]
        # The collision block is hidden (group 3); a top and four legs show the table.
        table.group = 3
        table.rgba = [0.62, 0.52, 0.42, 0.0]
        top = world.add_geom()
        top.type = mujoco.mjtGeom.mjGEOM_BOX
        top.size = [TABLE_BOUND_M, TABLE_BOUND_M, 0.015]
        top.pos = [cx, cy, -0.015]
        top.rgba = [0.62, 0.52, 0.42, 1.0]
        top.contype = top.conaffinity = 0
        for sx in (-1, 1):
            for sy in (-1, 1):
                leg = world.add_geom()
                leg.type = mujoco.mjtGeom.mjGEOM_BOX
                leg.size = [0.02, 0.02, (TABLE_HEIGHT_M - 0.03) / 2]
                leg.pos = [cx + sx * 0.22, cy + sy * 0.22, -(TABLE_HEIGHT_M + 0.03) / 2]
                leg.rgba = [0.45, 0.37, 0.3, 1.0]
                leg.contype = leg.conaffinity = 0

    size = case.box_size_m
    target = world.add_geom()
    target.name = "target_marker"
    target.type = mujoco.mjtGeom.mjGEOM_BOX
    target.size = [size / 2, size / 2, 0.0005]
    target.pos = [case.target_xy_m[0], case.target_xy_m[1], 0.0005]
    target.rgba = [0.2, 0.45, 1.0, 0.45]
    target.contype = 0
    target.conaffinity = 0
    target.group = 1

    box = world.add_body()
    box.name = "box"
    box.pos = [case.initial_xy_m[0], case.initial_xy_m[1], size / 2 + BOX_DROP_M]
    box.quat = _yaw_quat(math.radians(case.initial_yaw_deg))
    box.add_freejoint()
    geom = box.add_geom()
    geom.name = "box"
    geom.type = mujoco.mjtGeom.mjGEOM_BOX
    geom.size = [size / 2, size / 2, size / 2]
    geom.density = BOX_DENSITY
    # The 2F-85 holds a small box on two narrow pads: torsional friction, as rubber pads give,
    # keeps it from spinning in the grip when the wrist turns it to the target yaw.
    geom.condim = 4
    geom.friction = list(BAR_FRICTION)
    geom.rgba = [0.12, 0.7, 0.25, 1.0]

    light = world.add_light()
    light.pos = [0.4, -0.3, 2.0]
    light.dir = [0.0, 0.0, -1.0]
    return spec.compile()


def _base_pose(pose: Any) -> dict[str, float]:
    x, y, yaw = (float(v) for v in pose)
    return {"x_m": round(x, 6), "y_m": round(y, 6), "yaw_deg": round(math.degrees(yaw), 6)}


class TidybotWorld(SimWorld):
    """Physics behind the five ``fetch-v1`` tools: drive, pick, drive, place."""

    TOOLS = FETCH_TOOLS
    ACTUATING = ACTUATING_TOOLS | {"go_to"}
    AUDITED = ("pick", "go_to", "place", "abort")
    #: Reach from the base (see ``REACH_M``), and seconds to carry the box over the target.
    REACH = REACH_M
    REACH_BEARING = REACH_BEARING_DEG
    PLACE_CARRY = PLACE_CARRY_S

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
    ) -> None:
        joints = [model.joint(name).id for name in BASE_JOINTS]
        self._base_qpos = np.array([model.jnt_qposadr[j] for j in joints])
        self._base_ctrl = np.array([model.actuator(name).id for name in BASE_JOINTS])
        self._table_b = model.geom("table_b").id
        self.drives = 0
        super().__init__(case, clock, model, frame_hook, RIG)
        self.kin.obstacles.add(self._table_b)
        # Both drivers open (the engine would put the second listed finger joint at its upper
        # limit, which for the 2F-85 is closed).
        self.kin.open_fingers = [0.0 for _ in self.kin.finger_qpos]
        self._sync_kinematics()

    def handlers(self) -> dict[str, Any]:
        return {**super().handlers(), "go_to": self.go_to}

    # --------------------------------------------------------------------------- physics
    def _reset(self) -> None:
        x, y, yaw = START_POSE
        start = np.array([x, y, math.radians(yaw)])
        self.data.qpos[self._base_qpos] = start
        self.data.ctrl[self._base_ctrl] = start
        super()._reset()

    def _forbidden(self, a: int, b: int) -> bool:
        pair = {a, b}
        if self._box in pair and pair & {self._table, self._table_b}:
            # The tables are solid blocks: the box may rest on a top, never hit a side.
            return self.box_z() < self.case.box_size_m / 2 - RESTING_TOLERANCE_M
        return super()._forbidden(a, b)

    def base(self) -> Any:
        """The base pose the physics holds: x, y (m) and heading (rad)."""
        return self.data.qpos[self._base_qpos].copy()

    def _station(self, x: float, y: float) -> tuple[float, float, float] | None:
        """Base pose (x, y, heading degrees) to work at (x, y), or ``None`` off every table."""
        return station(x, y)

    def _put_base(self, data: Any, pose: Any) -> None:
        """Set the base pose (x, y, heading) in ``data``."""
        data.qpos[self._base_qpos] = pose

    def _sync_kinematics(self) -> None:
        """Move the planning model's base to where the base is."""
        pose = self.base()
        self._put_base(self.kin.data, pose)
        self.kin.base_xy = (float(pose[0]), float(pose[1]))
        self.kin.base_yaw = float(pose[2])

    def _gripper_holding(self, arm: Any = None) -> bool:
        rig = self.rig
        closed = float(self.data.ctrl[self._grip]) > (rig.grip_open + rig.grip_closed) / 2
        return closed and float(self.data.qpos[self._gripper_qpos]) < HOLDING_BELOW_RAD

    def _goal(self) -> tuple[float, float, float, float] | None:
        return None  # the target is placed from another base pose: plan grasps without it

    def within_reach(self, x: float, y: float) -> bool:
        if table_under(x, y) is None:
            return False
        bx, by, heading = (float(v) for v in self.base())
        distance = math.hypot(x - bx, y - by)
        bearing = math.degrees(arm.wrap(math.atan2(y - by, x - bx) - heading))
        return self.REACH[0] <= distance <= self.REACH[1] and abs(bearing) <= self.REACH_BEARING

    def _observation_extra(self) -> dict[str, Any]:
        bx, by = self.box_xy()
        dx, dy, _ = ESTIMATE_OFFSET
        target_x, target_y = self.case.target_xy_m
        return {
            "base_pose": _base_pose(self.base()),
            "within_reach": {
                "object": self.within_reach(bx + dx, by + dy),
                "target": self.within_reach(target_x, target_y),
            },
        }

    def _base_path_clear(self, start: Any, goal: Any) -> bool:
        """No robot contact with a table or the floor at sampled base poses on the straight drive
        from ``start`` to ``goal``, with the arm as it is now."""
        kin, data = self.kin, self.kin.data
        data.qpos[kin.qpos] = self.data.qpos[kin.qpos]
        count = max(2, math.ceil(float(np.linalg.norm(goal[:2] - start[:2])) * PATH_SAMPLES_PER_M))
        clear = True
        for k in range(count + 1):
            self._put_base(data, start + (goal - start) * k / count)
            mujoco.mj_kinematics(self.model, data)
            mujoco.mj_collision(self.model, data)
            for i in range(data.ncon):
                pair = {int(data.contact[i].geom1), int(data.contact[i].geom2)}
                if pair & kin.robot_geoms and pair & kin.obstacles:
                    clear = False
        self._sync_kinematics()
        return clear

    def _drive(self, goal: Any) -> None:
        start = self.data.ctrl[self._base_ctrl].copy()
        distance = float(np.linalg.norm(goal[:2] - start[:2]))
        turn = abs(float(goal[2] - start[2]))
        seconds = max(1.0, distance / DRIVE_SPEED_M_S, turn / TURN_SPEED_RAD_S)
        count = self._steps(seconds)
        for k in range(count):
            self.data.ctrl[self._base_ctrl] = start + (goal - start) * _smoothstep((k + 1) / count)
            self._step(1)
        self._hold(0.3)
        self._sync_kinematics()

    def _raised(self, q: Any, lift: float) -> Any:
        """Joint positions that put the tool site ``lift`` higher than at ``q`` with the jaws
        turned the same, or ``None``."""
        data = self.kin.data
        data.qpos[self.kin.qpos] = q
        mujoco.mj_kinematics(self.model, data)
        position = data.site_xpos[self.kin.site] + np.array([0.0, 0.0, lift])
        frame = data.site_xmat[self.kin.site].reshape(3, 3)
        return self.kin.solve(position, math.atan2(frame[1, 2], frame[0, 2]), q)

    def _travel_pose(self) -> Any:
        """The arm pose to drive with: the box ``TRAVEL_LIFT_M`` above where it was picked up
        when holding, otherwise ready."""
        if not self.held or self.grasp is None:
            return np.array(self.rig.ready)
        raised = self._raised(self.grasp.carry, TRAVEL_LIFT_M)
        return self.grasp.carry if raised is None else raised

    def _plan_place(
        self, x: float, y: float, z: float, yaw_deg: float
    ) -> tuple[arm.Placement | None, Any]:
        """A vertical place at (x, y, z, yaw) planned from where the arm is now (raised for
        travel): over the target at travel height, down to the approach, then the release. Each
        pose is seeded from the one before, so the arm stays in one configuration and the box it
        carries is not swept through the tabletop."""
        assert self.grasp is not None
        kin, rig = self.kin, self.rig
        jaw_yaw = arm.wrap(self.grasp.jaw_yaw + math.radians(yaw_deg) - self.grasp.box_yaw)
        height = max(z, rig.min_site_z_m) + arm.PLACE_CLEARANCE_M
        site = arm.site_position(rig, (x, y, height), jaw_yaw, self.grasp.box_size)
        current = self.data.qpos[kin.qpos].copy()
        over = kin.solve(site + np.array([0.0, 0.0, TRAVEL_LIFT_M]), jaw_yaw, current)
        if over is None:
            return None, None
        approach = kin.first(site, jaw_yaw, arm.CARRY_HEIGHTS_M, over)
        release = None if approach is None else kin.solve(site, jaw_yaw, approach)
        if release is None or not kin.path_clear(current, over, approach, release):
            return None, None
        return arm.Placement(approach, release), over

    def _replan_place(self, placement: arm.Placement, args: dict[str, Any]) -> arm.Placement:
        """The descent and release to use once the arm is over the target (as planned here)."""
        return placement

    # ------------------------------------------------------------------------------ tools
    def go_to(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m"))
        planned = None if reason is not None else self._station(args["x_m"], args["y_m"])
        goal: Any = None
        if reason is None and planned is not None:
            x, y, yaw = planned
            goal = np.array([x, y, math.radians(yaw)])
        if reason is None and (goal is None or not self._base_path_clear(self.base(), goal)):
            reason = "unreachable_target"
        entry = self._log("go_to", reason, context)
        if reason is not None or goal is None:
            return self._reply("rejected", reason=reason)
        self.drives += 1
        travel = self._travel_pose()
        if not np.allclose(travel, self.data.ctrl[self._arm]):
            self._move(travel, APPROACH_S)
        short = self.drives <= self.case.stopped_short_drives
        start = self.base()
        self._drive(start + (goal - start) * STOP_SHORT_FRACTION if short else goal)
        entry["effect"] = True
        if short:
            self._last_result = "stopped_short"
            return self._reply("error", base_pose=_base_pose(self.base()), reason="stopped_short")
        self._last_result = "arrived"
        return self._reply("ok", base_pose=_base_pose(self.base()))

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "yaw_deg", "grasp_height_m"))
        if reason is None and not self.within_reach(args["x_m"], args["y_m"]):
            self._log("pick", "unreachable_target", context)
            return self._reply("rejected", reason="unreachable_target")
        self._sync_kinematics()
        return super().pick(args)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
        if reason is None and not self.within_reach(args["x_m"], args["y_m"]):
            reason = "unreachable_target"
        if reason is None and (not self.held or self.grasp is None):
            reason = "not_held"
        placement: arm.Placement | None = None
        over: Any = None
        if reason is None and self.grasp is not None:
            self._sync_kinematics()
            placement, over = self._plan_place(
                args["x_m"], args["y_m"], args["z_m"], args["yaw_deg"]
            )
            if placement is None:
                reason = "unreachable_target"
        entry = self._log("place", reason, context)
        if reason is not None or placement is None:
            return self._reply("rejected", reason=reason)
        before = self.box_pose()
        self._move(over, self.PLACE_CARRY)
        placement = self._replan_place(placement, args)
        self._move(placement.approach, DESCEND_S)
        self._move(placement.release, DESCEND_S)
        self._move(placement.release, RELEASE_S, grip=self.rig.grip_open)
        self.held = False
        self.grasp = None
        self.released_ms = self.clock()
        self._hold(0.2)
        self._move(placement.approach, RETREAT_S)
        entry["effect"] = self._displaced(before)
        self._last_result = "released"
        return self._reply("ok", released=True)

    def abort(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args:
            self.data.ctrl[self._base_ctrl] = self.base()
        return super().abort(args)

    def final_state(self) -> dict[str, Any]:
        state = super().final_state()
        state["base_pose"] = _base_pose(self.base())
        return state


def new_tidybot_world(
    case: FixtureCase, clock: FixtureClock, assets: Path, frame_hook: FrameHook | None = None
) -> TidybotWorld:
    """Build the room for ``case`` and wrap it in a ``TidybotWorld``."""
    return TidybotWorld(case, clock, build_model(case, assets), frame_hook)


def load_tidybot_cases(path: Path = CASES_FILE) -> tuple[FixtureCase, ...]:
    """The frozen TidyBot case set (``uzumaki.baseline-cases/1.0``, protocol ``fetch-v1``)."""
    return load_cases(path, FETCH_PROTOCOL)


def tidybot_environment(
    asset_dir: Path, frame_hook: FrameHook | None = None
) -> Callable[[FixtureCase], CaseEnvironment]:
    """Environment factory for ``run_case``: a fresh room, robot and evaluator per case."""

    def build(case: FixtureCase) -> CaseEnvironment:
        clock = FixtureClock()
        world = new_tidybot_world(case, clock, asset_dir, frame_hook)
        return CaseEnvironment(clock, world, SimEvaluator(world))

    return build


#: Seed recipe ranges (SHA-256 as in ``pick-place-v1``), room frame: box size m, box centre on
#: table ``a``, target on table ``b`` (x, y m) and yaw degrees. Both lie 0.05 to 0.37 m in from
#: the table's -y edge, where the arm reaches from the station.
RANGES = {
    "size": (0.03, 0.06),
    "initial_x": (-0.15, 0.15),
    "initial_y": (-0.2, 0.12),
    "target_x": (0.65, 0.95),
    "target_y": (-0.2, 0.12),
    "yaw": (-45.0, 45.0),
}
#: A target on no table, between the two (the ``unreachable-goal`` case).
UNREACHABLE_TARGET = [0.4, 0.0]


def _draw(seed: str, label: str) -> float:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    low, high = RANGES[label]
    return round(low + (high - low) * int.from_bytes(digest[:8], "big") / (2**64 - 1), 6)


def generate_geometry(seed: str) -> dict[str, Any]:
    return {
        "box_size_m": _draw(seed, "size"),
        "initial_xy_m": [_draw(seed, "initial_x"), _draw(seed, "initial_y")],
        "initial_yaw_deg": _draw(seed, "yaw"),
        "target_xy_m": [_draw(seed, "target_x"), _draw(seed, "target_y")],
    }


_NO_FAULTS = {
    "misgrasp_picks": 0,
    "stale_observes": [],
    "crash_on_call": None,
    "withdraw_goal_after_call": None,
}


def _expected(
    primary: str, progress: str, status: str, attempts: int, *admitted: int
) -> dict[str, Any]:
    state = {"cancelled": "cancelled", "failed": "failed"}.get(status, "completed")
    go_to, pick, place = admitted
    return {
        "primary": primary,
        "progress": progress,
        "state": state,
        "status": status,
        "attempts": attempts,
        "admitted": {"go_to": go_to, "pick": pick, "place": place},
    }


#: The eight fault cases: the SO-101's, with ``stopped-short`` in place of ``stale-after-pick``
#: (call numbers: observe 1, go_to 2, observe 3, pick 4, observe 5, go_to 6, observe 7, place 8).
FAULT_CASES: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("nominal", {}, _expected("completed", "goal_reached", "completed", 1, 2, 1, 1)),
    ("unreachable-goal", {}, _expected("unreachable_target", "partial", "blocked", 1, 1, 1, 0)),
    (
        "failed-grasp",
        {"misgrasp_picks": 1},
        _expected("completed", "goal_reached", "completed", 2, 2, 2, 1),
    ),
    (
        "stopped-short",
        {"stopped_short_drives": 1},
        _expected("completed", "goal_reached", "completed", 2, 3, 1, 1),
    ),
    (
        "stale-observation",
        {"stale_observes": [1]},
        _expected("completed", "goal_reached", "completed", 2, 2, 1, 1),
    ),
    (
        "retry-exhausted",
        {"misgrasp_picks": 3},
        _expected("budget_exhausted", "none", "budget_exhausted", 3, 1, 3, 0),
    ),
    (
        "cancellation",
        {"withdraw_goal_after_call": 4},
        _expected("cancelled", "partial", "cancelled", 1, 1, 1, 0),
    ),
    (
        "worker-crash",
        {"crash_on_call": 5},
        _expected("evaluation_failure", "partial", "failed", 1, 1, 1, 0),
    ),
)


def build_case_set() -> dict[str, Any]:
    """The TidyBot case set: eight fault cases on seeds ``tidybot-1001`` to ``1008`` and twenty
    nominal cases on ``tidybot-1009`` to ``1028`` (``uzumaki.baseline-cases/1.0``)."""
    cases = []
    for index in range(28):
        geometry = f"tidybot-dev-{index + 1:03d}"
        values = generate_geometry(f"tidybot-{1001 + index}")
        if index < len(FAULT_CASES):
            case_id, faults, expected = FAULT_CASES[index]
        else:
            case_id, faults, expected = geometry, {}, FAULT_CASES[0][2]
        if case_id == "unreachable-goal":
            values["target_xy_m"] = list(UNREACHABLE_TARGET)
            geometry += "-target-between-tables"
        cases.append(
            {
                "case_id": case_id,
                "geometry": geometry,
                **values,
                "faults": {**_NO_FAULTS, **faults},
                "expected": copy.deepcopy(expected),
            }
        )
    return {"format": "uzumaki.baseline-cases/1.0", "protocol": FETCH_PROTOCOL, "cases": cases}
