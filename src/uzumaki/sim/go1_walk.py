"""Unitree Go1 that walks, with a jaw on its neck: it fetches a box from one table to another
(``fetch-v1``).

The room is TidyBot's (``uzumaki.fetch_env``: two 0.5 x 0.5 m tables, table ``a`` at the origin
and table ``b`` 0.8 m along x, each worked from its -y edge), with low tables ``TABLE_HEIGHT_M``
high at the dog's shoulder. The robot is the Go1 of ``uzumaki.sim.go1`` (the same six-joint neck
and UMI jaw, built by ``go1.add_neck_and_jaw``) on the legs of a learned walking policy:

- **The model and the policy** are MuJoCo Playground's, pinned (``assets.GO1_WALK_MODEL``): the
  Go1 model its joystick policy was trained on (position-controlled legs, collisions on the feet
  only) and that policy in ONNX format, run with ``onnxruntime``. Every 20 ms the policy reads
  the trunk's velocity, rotation rate and tilt, the leg joints and its last action, plus a
  velocity command (forward, sideways, turn), and sets the twelve leg targets.
- **Walking.** ``go_to`` admits a point on a table, as TidyBot's does, and walks to that table's
  work station (``fetch_env.station``): a proportional controller turns the remaining distance
  and heading into the policy's velocity command until the trunk is within ``ARRIVED_M`` and
  ``ARRIVED_DEG`` of the station. The dog then stands for ``SETTLE_S`` on a zero command, and
  its legs hold that stance while the neck works: ``pick`` and ``place`` plan from the pose the
  trunk actually holds.
- **Collisions.** The policy's model collides only through its feet. Here the legs and trunk also
  collide with the tables (not with the floor, which the policy expects), so walking into a
  table is a forbidden contact, as is any robot contact with a table or the floor except the feet
  on the floor.

``Go1WalkWorld`` is TidyBot's ``TidybotWorld`` with the legs in place of the wheels; the fault
``stopped_short_drives`` stops a walk ``STOP_SHORT_FRACTION`` of the way. The case set is
TidyBot's seed recipe on its own seeds (``build_case_set``).
"""

from __future__ import annotations

import copy
import dataclasses
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from uzumaki.baseline import CaseEnvironment
from uzumaki.fetch_env import START_POSE, TABLES, table_under
from uzumaki.fixture_env import (
    FETCH_PROTOCOL,
    TABLE_BOUND_M,
    FixtureCase,
    FixtureClock,
    load_cases,
)
from uzumaki.sim import SimUnavailable, arm, go1, tidybot
from uzumaki.sim.assets import GO1_MODEL, GO1_WALK_MODEL, GO1_WALK_POLICY
from uzumaki.sim.scene import BOX_DENSITY, BOX_DROP_M, BOX_FRICTION, TIMESTEP_S, _yaw_quat
from uzumaki.sim.world import FrameHook, SimEvaluator, SimWorld

CASES_FILE = Path(__file__).parent / "data" / "go1_walk_cases_v1.json"
CASE_SET = "go1-walk-dev-v1"
TABLE_HEIGHT_M = go1.TABLE_HEIGHT_M
FLOOR_Z_M = -TABLE_HEIGHT_M
#: The policy's ``home`` stance (MuJoCo Playground's keyframe): trunk height above the floor and
#: the twelve leg joints (FR, FL, RR, RL: hip, thigh, calf). Its actions are offsets from it.
HOME_HEIGHT_M = 0.278
HOME = (0.1, 0.9, -1.8, -0.1, 0.9, -1.8, 0.1, 0.9, -1.8, -0.1, 0.9, -1.8)
LEG_ACTUATORS = tuple(n.removesuffix("_joint") for n in go1.LEG_JOINTS)
FEET = ("FR", "FL", "RR", "RL")
ACTION_SCALE = 0.5
#: The policy acts every 20 ms: every fourth 5 ms physics step.
POLICY_STEPS = 4
#: Walking. The policy stands still for commands slower than about 0.3 m/s or 0.5 rad/s, so it
#: walks toward the station at ``WALK_SPEED_M_S`` (``GAIN`` times the remaining distance, within
#: those limits) until within ``ARRIVED_M``, and turns in place at ``TURN_RAD_S`` until the
#: heading is within ``ARRIVED_DEG``; it walks for at most ``WALK_LIMIT_S``, then stands for
#: ``SETTLE_S`` on a zero command.
WALK_SPEED_M_S = (0.35, 0.45)
GAIN = 2.0
TURN_RAD_S = 0.8
ARRIVED_M = 0.03
ARRIVED_DEG = 3.0
WALK_LIMIT_S = 20.0
SETTLE_S = 1.0
#: Work station: the trunk centre this far from the table's -y edge, facing +y (TidyBot's base
#: stands 0.27 m off; the dog's front feet swing forward of its shoulders).
STANDOFF_M = 0.30
STOP_SHORT_FRACTION = tidybot.STOP_SHORT_FRACTION
#: Reach from the trunk centre (m) and its heading (degrees) for pick and place.
REACH_M = (0.3, 0.72)
REACH_BEARING_DEG = 20.0
#: Collision bits: legs and trunk touch tables (``LEG_BIT``) but not the floor.
LEG_BIT = 2

RIG = dataclasses.replace(
    go1.RIG,
    name="go1_walk",
    assets=GO1_WALK_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    base_xy=(START_POSE[0], START_POSE[1]),
    base_yaw_deg=START_POSE[2],
    base_z_m=FLOOR_Z_M + HOME_HEIGHT_M,
    protocol=FETCH_PROTOCOL,
    camera=(0.4, -0.3, 0.0, 1.7, -115.0, -30.0),
    customize=None,
    world=None,
)


def build_model(case: FixtureCase, assets: Path) -> Any:
    """Compile the room for ``case``: the walking Go1 with its neck and jaw, two tables, the box
    on table ``a`` and a visual-only target marker."""
    spec = mujoco.MjSpec.from_file(str(assets / GO1_WALK_MODEL.entry))
    for mesh in spec.meshes:
        mesh.file = Path(mesh.file).name
    spec.meshdir = str(GO1_MODEL.verify() / "assets")
    spec.option.timestep = TIMESTEP_S
    # The policy's model is tuned for one solver iteration; the neck's stiff servos need the
    # default solver.
    spec.option.iterations = 100
    spec.option.ls_iterations = 50
    spec.visual.global_.offwidth = 1280
    spec.visual.global_.offheight = 960
    spec.visual.headlight.ambient = [0.35, 0.35, 0.35]
    spec.visual.headlight.diffuse = [0.55, 0.55, 0.55]
    for geom in spec.geoms:
        if geom.name not in FEET and geom.group == 3:
            geom.contype = LEG_BIT
    trunk = spec.body("trunk")
    trunk.pos = [RIG.base_xy[0], RIG.base_xy[1], RIG.base_z_m]
    trunk.quat = _yaw_quat(math.radians(RIG.base_yaw_deg))
    go1.add_neck_and_jaw(spec)
    for name in ("neck_base", "upper_neck", "lower_neck", "head", "jaw_mount"):
        for geom in spec.body(name).geoms:
            geom.contype = geom.conaffinity = 1
            geom.condim = 3
    world = spec.worldbody

    floor = world.add_geom()
    floor.name = "floor"
    floor.type = mujoco.mjtGeom.mjGEOM_PLANE
    floor.size = [3.0, 3.0, 0.05]
    floor.pos = [0.4, 0.0, FLOOR_Z_M]
    floor.rgba = [0.18, 0.2, 0.24, 1.0]
    # As the policy was trained: feet only (contype 1 against their conaffinity 1).
    floor.contype = 1
    floor.conaffinity = 0
    floor.priority = 1
    floor.friction = [0.6, 0.005, 0.0001]
    floor.condim = 3
    for name, (cx, cy) in TABLES.items():
        table = world.add_geom()
        table.name = "table" if name == "a" else f"table_{name}"
        table.type = mujoco.mjtGeom.mjGEOM_BOX
        table.size = [TABLE_BOUND_M, TABLE_BOUND_M, TABLE_HEIGHT_M / 2]
        table.pos = [cx, cy, -TABLE_HEIGHT_M / 2]
        table.contype = 1
        table.conaffinity = 1 | LEG_BIT
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
    geom.friction = list(BOX_FRICTION)
    geom.contype = 1
    geom.conaffinity = 1 | LEG_BIT
    geom.rgba = [0.12, 0.7, 0.25, 1.0]

    light = world.add_light()
    light.pos = [0.4, -0.3, 2.0]
    light.dir = [0.0, 0.0, -1.0]
    return spec.compile()


class WalkingPolicy:
    """MuJoCo Playground's Go1 joystick policy (ONNX): observations in, leg targets out."""

    def __init__(self, path: Path, model: Any) -> None:
        try:
            import onnxruntime
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise SimUnavailable(
                "onnxruntime is not installed (pip install -r requirements-sim.txt)"
            ) from error
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            str(path), options, providers=["CPUExecutionProvider"]
        )
        ids = [model.actuator(n).id for n in LEG_ACTUATORS]
        joints = [int(model.actuator_trnid[i][0]) for i in ids]
        self.ctrl = np.array(ids)
        self.qpos = np.array([model.jnt_qposadr[j] for j in joints])
        self.dofs = np.array([model.jnt_dofadr[j] for j in joints])
        self.imu = model.site("imu").id
        self.home = np.array(HOME)
        self.last = np.zeros(len(HOME), dtype=np.float32)

    def act(self, data: Any, command: Any) -> None:
        """Set the leg targets in ``data.ctrl`` for velocity ``command`` (forward m/s, sideways
        m/s, turn rad/s in the trunk frame)."""
        imu = data.site_xmat[self.imu].reshape(3, 3)
        observation = np.hstack(
            [
                data.sensor("local_linvel").data,
                data.sensor("gyro").data,
                imu.T @ np.array([0.0, 0.0, -1.0]),
                data.qpos[self.qpos] - self.home,
                data.qvel[self.dofs],
                self.last,
                command,
            ]
        ).astype(np.float32)
        action = self.session.run(["continuous_actions"], {"obs": observation.reshape(1, -1)})
        self.last = np.asarray(action[0][0], dtype=np.float32)
        data.ctrl[self.ctrl] = self.home + ACTION_SCALE * self.last


def _yaw(quat: Any) -> float:
    w, x, y, z = (float(v) for v in quat)
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class Go1WalkWorld(tidybot.TidybotWorld):
    """Physics behind the five ``fetch-v1`` tools for the walking Go1: walk, pick, walk, place."""

    REACH = REACH_M
    REACH_BEARING = REACH_BEARING_DEG
    PLACE_CARRY = 2.0

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
    ) -> None:
        self.policy = WalkingPolicy(GO1_WALK_MODEL.verify() / GO1_WALK_POLICY, model)
        root = model.body("trunk").jntadr[0]
        self._root_qpos = int(model.jnt_qposadr[root])
        self._fingers = np.array([model.jnt_qposadr[model.joint(n).id] for n in go1.FINGERS])
        feet = {model.geom(n).id for n in FEET}
        self._feet = feet
        self._table_b = model.geom("table_b").id
        #: Velocity command while the policy drives the legs; ``None`` while they hold a stance.
        self.command: Any = None
        self._tick = 0
        self.drives = 0
        SimWorld.__init__(self, case, clock, model, frame_hook, RIG)
        self.kin.robot_geoms -= feet
        self.kin.obstacles.add(self._table_b)
        self.kin.open_fingers = [go1.GRIP_OPEN, go1.GRIP_OPEN]
        self._sync_kinematics()

    # --------------------------------------------------------------------------- physics
    def _reset(self) -> None:
        x, y, yaw = START_POSE
        adr = self._root_qpos
        self.data.qpos[adr : adr + 3] = [x, y, FLOOR_Z_M + HOME_HEIGHT_M]
        self.data.qpos[adr + 3 : adr + 7] = _yaw_quat(math.radians(yaw))
        self.data.qpos[self.policy.qpos] = HOME
        self.data.ctrl[self.policy.ctrl] = HOME
        self.kin.open_fingers = [go1.GRIP_OPEN, go1.GRIP_OPEN]
        SimWorld._reset(self)

    def _step(self, count: int) -> None:
        for _ in range(count):
            if self.command is not None and self._tick % POLICY_STEPS == 0:
                self.policy.act(self.data, self.command)
            self._tick += 1
            super()._step(1)

    def _forbidden(self, a: int, b: int) -> bool:
        if {a, b} & self._feet and self._floor in (a, b):
            return False
        return super()._forbidden(a, b)

    def base(self) -> Any:
        adr = self._root_qpos
        qpos = self.data.qpos
        return np.array([qpos[adr], qpos[adr + 1], _yaw(qpos[adr + 3 : adr + 7])])

    def _put_base(self, data: Any, pose: Any) -> None:
        adr = self._root_qpos
        data.qpos[adr : adr + 2] = pose[:2]
        data.qpos[adr + 3 : adr + 7] = _yaw_quat(float(pose[2]))

    def _sync_kinematics(self) -> None:
        self.kin.data.qpos[:] = self.data.qpos
        x, y, yaw = (float(v) for v in self.base())
        self.kin.base_xy = (x, y)
        self.kin.base_yaw = yaw

    def _gripper_holding(self, arm: Any = None) -> bool:
        closed = float(self.data.ctrl[self._grip]) > (go1.GRIP_OPEN + go1.GRIP_CLOSED) / 2
        return closed and float(np.mean(self.data.qpos[self._fingers])) < go1.HOLDING_BELOW_M

    def _station(self, x: float, y: float) -> tuple[float, float, float] | None:
        table = table_under(x, y)
        if table is None:
            return None
        return x, TABLES[table][1] - TABLE_BOUND_M - STANDOFF_M, 90.0

    def _walk_command(self, goal: Any) -> Any:
        """The velocity command toward ``goal`` (x, y, heading), or ``None`` on arrival."""
        x, y, yaw = (float(v) for v in self.base())
        dx, dy = float(goal[0]) - x, float(goal[1]) - y
        distance = math.hypot(dx, dy)
        turn = arm.wrap(float(goal[2]) - yaw)
        command = np.zeros(3, dtype=np.float32)
        if distance >= ARRIVED_M:
            speed = float(np.clip(GAIN * distance, *WALK_SPEED_M_S))
            command[0] = speed * (math.cos(yaw) * dx + math.sin(yaw) * dy) / distance
            command[1] = speed * (-math.sin(yaw) * dx + math.cos(yaw) * dy) / distance
        elif abs(math.degrees(turn)) >= ARRIVED_DEG:
            command[2] = math.copysign(TURN_RAD_S, turn)
        else:
            return None
        return command

    def _drive(self, goal: Any) -> None:
        """Walk to ``goal`` (x, y, heading) with the policy, stand, then hold the stance."""
        for _ in range(self._steps(WALK_LIMIT_S) // POLICY_STEPS):
            self.command = self._walk_command(goal)
            if self.command is None:
                break
            self._step(POLICY_STEPS)
        self.command = np.zeros(3, dtype=np.float32)
        self._hold(SETTLE_S)
        self.command = None
        self._hold(0.3)
        self._sync_kinematics()

    def _replan_grasp(self, grasp: arm.Grasp) -> arm.Grasp:
        """Over the box the neck's reach has tilted the trunk on its soft legs a little: plan the
        grasp and lift again from the pose the trunk now holds (keeping the first plan if that
        fails)."""
        kin = self.kin
        # The planning model still holds the trunk pose the grasp was planned from.
        kin.data.qpos[kin.qpos] = grasp.grasp
        mujoco.mj_kinematics(self.model, kin.data)
        site = kin.data.site_xpos[kin.site].copy()
        self._sync_kinematics()
        hover = self.data.qpos[kin.qpos].copy()
        closed = kin.solve(site, grasp.jaw_yaw, grasp.grasp)
        carry = None
        if closed is not None:
            carry = kin.first(site, grasp.jaw_yaw, arm.CARRY_HEIGHTS_M, closed)
        if carry is None or not kin.path_clear(hover, closed, carry):
            return grasp
        return dataclasses.replace(grasp, grasp=closed, carry=carry)

    def _replan_place(self, placement: arm.Placement, args: dict[str, Any]) -> arm.Placement:
        """Over the target the neck's reach has tilted the trunk on its soft legs a little: plan
        the descent and release again from the pose the trunk now holds (keeping the first plan
        if that fails)."""
        assert self.grasp is not None
        self._sync_kinematics()
        kin = self.kin
        jaw_yaw = arm.wrap(self.grasp.jaw_yaw + math.radians(args["yaw_deg"]) - self.grasp.box_yaw)
        height = max(args["z_m"], self.rig.min_site_z_m) + arm.PLACE_CLEARANCE_M
        site = arm.site_position(
            self.rig, (args["x_m"], args["y_m"], height), jaw_yaw, self.grasp.box_size
        )
        over = self.data.qpos[kin.qpos].copy()
        approach = kin.first(site, jaw_yaw, arm.CARRY_HEIGHTS_M, placement.approach)
        release = None if approach is None else kin.solve(site, jaw_yaw, placement.release)
        if release is None or not kin.path_clear(over, approach, release):
            return placement
        return arm.Placement(approach, release)

    # ------------------------------------------------------------------------------ tools
    def abort(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args:
            self.command = None
        return SimWorld.abort(self, args)


def new_go1_walk_world(
    case: FixtureCase, clock: FixtureClock, assets: Path, frame_hook: FrameHook | None = None
) -> Go1WalkWorld:
    """Build the room for ``case`` and wrap it in a ``Go1WalkWorld``."""
    return Go1WalkWorld(case, clock, build_model(case, assets), frame_hook)


def load_go1_walk_cases(path: Path = CASES_FILE) -> tuple[FixtureCase, ...]:
    """The frozen walking-Go1 case set (``uzumaki.baseline-cases/1.0``, protocol ``fetch-v1``)."""
    return load_cases(path, FETCH_PROTOCOL)


def go1_walk_environment(
    asset_dir: Path, frame_hook: FrameHook | None = None
) -> Callable[[FixtureCase], CaseEnvironment]:
    """Environment factory for ``run_case``: a fresh room, robot and evaluator per case."""

    def build(case: FixtureCase) -> CaseEnvironment:
        clock = FixtureClock()
        world = new_go1_walk_world(case, clock, asset_dir, frame_hook)
        return CaseEnvironment(clock, world, SimEvaluator(world))

    return build


def build_case_set() -> dict[str, Any]:
    """The walking-Go1 case set: TidyBot's construction (``tidybot.build_case_set``) on seeds
    ``go1-walk-1001`` to ``1028`` (``uzumaki.baseline-cases/1.0``)."""
    cases = []
    for index in range(28):
        geometry = f"go1-walk-dev-{index + 1:03d}"
        values = tidybot.generate_geometry(f"go1-walk-{1001 + index}")
        if index < len(tidybot.FAULT_CASES):
            case_id, faults, expected = tidybot.FAULT_CASES[index]
        else:
            case_id, faults, expected = geometry, {}, tidybot.FAULT_CASES[0][2]
        if case_id == "unreachable-goal":
            values["target_xy_m"] = list(tidybot.UNREACHABLE_TARGET)
            geometry += "-target-between-tables"
        cases.append(
            {
                "case_id": case_id,
                "geometry": geometry,
                **values,
                "faults": {**tidybot._NO_FAULTS, **faults},
                "expected": copy.deepcopy(expected),
            }
        )
    return {"format": "uzumaki.baseline-cases/1.0", "protocol": FETCH_PROTOCOL, "cases": cases}
