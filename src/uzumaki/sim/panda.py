"""The Franka Emika Panda as an ``ArmRig``: seven joints and a symmetric two-finger gripper.

The Menagerie model has no tool-centre site, so the rig adds ``tcp`` on the ``hand`` body between
the fingertip pads, with x along the approach (the hand's z) and z along the jaw axis (the hand's
y, the direction the fingers slide). The gripper is one tendon actuator driving both fingers
(control 0 closed to 255 open), so grasps centre the box between the fingers. With seven joints a
top-down pose leaves one degree of freedom; the seeds and a gentle null-space pull toward the
ready pose (the model's ``home`` keyframe) choose an elbow-up solution. Like the real arm's
controller, the rig compensates gravity, so the position actuators track without sagging.

The base stands just off the table edge at ``BASE_XY`` facing +y. From there the Panda reaches
top-down grasps over the whole tabletop except a corner strip next to the base, so unlike the
SO-101 it runs the frozen ``pick-place-v1`` geometry unchanged. Its case set ``CASES_FILE`` is the
eight frozen baseline fault cases exactly as they are (``tests/fixtures/baseline_cases_v1.json``)
plus one nominal case on each of the protocol's twenty development geometries ``dev-001`` to
``dev-020`` (``docs/protocol/task-v1.json``); ``build_case_set`` rebuilds it from those two files.
The held-out geometry stays with the protocol custodian.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from uzumaki.embodiments import PANDA
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import PANDA_MODEL

CASES_FILE = Path(__file__).parent / "data" / "panda_cases_v1.json"
CASE_SET = "panda-dev-v1"

BASE_XY = (0.0, -0.45)
BASE_YAW_DEG = 90.0
#: The model's ``home`` keyframe: hand pointing down, jaws along the arm's heading.
READY = (0.0, 0.0, 0.0, -1.57079, 0.0, 1.57079, -0.7853)


def _seeds(bearing: float, roll: float) -> list[Any]:
    # joint7 turns the jaws clockwise seen from above; at -pi/4 they lie along the bearing.
    wrist = -math.pi / 4 - roll
    return [
        np.array([bearing, shoulder, 0.0, elbow, 0.0, math.pi + elbow - shoulder, wrist])
        for shoulder, elbow in ((0.3, -2.2), (0.6, -1.6), (0.0, -2.6))
    ]


RIG = ArmRig(
    name="panda",
    robot=PANDA,
    assets=PANDA_MODEL,
    case_set=CASE_SET,
    cases_file=CASES_FILE,
    root_body="link0",
    base_xy=BASE_XY,
    base_yaw_deg=BASE_YAW_DEG,
    base_z_m=0.0,
    arm_actuators=tuple(f"actuator{i}" for i in range(1, 8)),
    gripper_actuator="actuator8",
    gripper_joint="finger_joint1",
    grip_open=255.0,
    grip_closed=0.0,
    closed_below=128.0,
    opening_above=0.005,
    ready=READY,
    site="tcp",
    site_body="hand",
    site_pos=(0.0, 0.0, 0.1034),
    site_quat=(0.5, -0.5, -0.5, -0.5),
    pad_bodies=("left_finger", "right_finger"),
    pad_priority=None,
    fixed_jaw_offset_m=0.0,
    grasp_gap_m=0.0,
    min_site_z_m=0.015,
    seeds=_seeds,
    roll_index=6,
    pan_index=0,
    gravity_compensation=True,
    pedestal_radius_m=0.09,
    camera=(0.0, -0.2, 0.3, 1.85, -60.0, -22.0),
)


def build_case_set(baseline: Path, protocol: Path) -> dict[str, Any]:
    """The Panda case set from the frozen baseline cases and the protocol's development
    geometry (``uzumaki.baseline-cases/1.0``)."""
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    nominal = next(case for case in frozen["cases"] if case["case_id"] == "nominal")
    geometries = json.loads(protocol.read_text(encoding="utf-8"))["profile"]["development_cases"]
    cases = list(frozen["cases"])
    for geometry in geometries:
        case = copy.deepcopy(nominal)
        case.update(
            case_id=f"panda-{geometry['id']}",
            geometry=geometry["id"],
            box_size_m=geometry["box_size_m"],
            initial_xy_m=geometry["initial_xy_m"],
            initial_yaw_deg=geometry["initial_yaw_deg"],
            target_xy_m=geometry["target_xy_m"],
        )
        cases.append(case)
    return {"format": frozen["format"], "protocol": frozen["protocol"], "cases": cases}
