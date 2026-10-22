"""MuJoCo scene for one case: the pinned SO-101, a tabletop, one box and a target marker.

The tabletop frame is the ``pick-place-v1`` goal frame: right-handed metres, tabletop at z = 0,
x/y bounds [-0.25, 0.25]. The arm base sits at ``BASE_XY`` facing +y. A floor 0.4 m below the
tabletop catches anything that falls, so a drop shows up as a forbidden contact. The target marker
is visual only (no collisions).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import mujoco

from uzumaki.fixture_env import TABLE_BOUND_M, FixtureCase
from uzumaki.sim.cases import BASE_XY, BASE_YAW_DEG

TABLE_THICKNESS_M = 0.02
FLOOR_Z_M = -0.4
BOX_DENSITY = 250.0  # kg/m^3: a 4.5 cm box weighs about 23 g
BOX_FRICTION = (1.0, 0.005, 0.0001)
#: Initial clearance so the box starts just above the tabletop and settles during reset.
BOX_DROP_M = 0.0005


def _yaw_quat(yaw_rad: float) -> list[float]:
    return [math.cos(yaw_rad / 2), 0.0, 0.0, math.sin(yaw_rad / 2)]


def build_model(case: FixtureCase, assets: Path) -> Any:
    """Compile the scene for ``case`` from verified assets (see ``uzumaki.sim.assets``)."""
    spec = mujoco.MjSpec.from_file(str(assets / "so101.xml"))
    spec.visual.global_.offwidth = 1280
    spec.visual.global_.offheight = 960
    base = spec.body("base")
    base.pos = [BASE_XY[0], BASE_XY[1], 0.0]
    base.quat = _yaw_quat(math.radians(BASE_YAW_DEG))
    world = spec.worldbody

    floor = world.add_geom()
    floor.name = "floor"
    floor.type = mujoco.mjtGeom.mjGEOM_PLANE
    floor.size = [1.0, 1.0, 0.05]
    floor.pos = [0.0, 0.0, FLOOR_Z_M]
    floor.rgba = [0.18, 0.2, 0.24, 1.0]

    table = world.add_geom()
    table.name = "table"
    table.type = mujoco.mjtGeom.mjGEOM_BOX
    table.size = [TABLE_BOUND_M, TABLE_BOUND_M, TABLE_THICKNESS_M / 2]
    table.pos = [0.0, 0.0, -TABLE_THICKNESS_M / 2]
    table.rgba = [0.62, 0.52, 0.42, 1.0]

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
    geom.rgba = [0.12, 0.7, 0.25, 1.0]

    light = world.add_light()
    light.pos = [0.0, -0.1, 1.5]
    light.dir = [0.0, 0.0, -1.0]
    return spec.compile()
