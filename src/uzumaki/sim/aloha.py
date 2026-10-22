"""ALOHA in MuJoCo: two ViperX 300 S arms that hand a bar from one to the other (``handover-v1``).

The arms face each other across the tabletop from x = -0.469 m (left, the giver) and x = +0.469 m
(right, the taker). Each is an ``ArmRig`` on the shared engine (``uzumaki.sim.arm``) with a
``tcp`` site added between its fingertips; the robot is the ``TwoArms`` description
``uzumaki.embodiments.ALOHA``. The object is a bar ``BAR_LENGTH_M`` long with a square
cross-section of the case's ``box_size_m``, so both grippers can hold it at once, one near each
end.

``AlohaWorld`` implements the five ``handover-v1`` tools on that physics. ``pick`` grasps the bar
with the giver, across the bar, ``GRASP_OFFSET_M`` from its centre on the giver's side, turned so
that each gripper's wrist camera mount faces away from the other arm at the meeting point.
``handover`` carries it to ``HANDOVER_XYZ`` (bar centre, bar along x), the taker grasps the other
end, the giver opens and withdraws, and the taker reports holding it, read from its gripper joint.
``place`` is the taker's. Planning uses only tool arguments and the goal. The case fault
``dropped_handovers`` makes the giver open before the taker has closed: the bar falls on the
tabletop, and the next attempt has to pick it up again. The evaluator is ``SimEvaluator``
unchanged; any contact between the two arms is robot self contact and ends the case as
``collision``.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from uzumaki.baseline import CaseEnvironment
from uzumaki.embodiments import ALOHA
from uzumaki.fixture_env import (
    ACTUATING_TOOLS,
    HANDOVER_PROTOCOL,
    HANDOVER_TOOLS,
    TABLE_BOUND_M,
    FixtureCase,
    FixtureClock,
    load_cases,
)
from uzumaki.robots import SingleArm
from uzumaki.sim import arm
from uzumaki.sim.arm import ArmRig
from uzumaki.sim.assets import ALOHA_MODEL
from uzumaki.sim.scene import build_model
from uzumaki.sim.world import (
    APPROACH_S,
    CARRY_S,
    CLOSE_S,
    DESCEND_S,
    GRIP_SETTLE_S,
    LIFT_S,
    RELEASE_S,
    RETREAT_S,
    ArmState,
    FrameHook,
    SimEvaluator,
    SimWorld,
)

CASES_FILE = Path(__file__).parent / "data" / "aloha_cases_v1.json"
CASE_SET = "aloha-dev-v1"
BAR_LENGTH_M = 0.24
#: Grasp point distance from the bar centre along the bar, toward the grasping arm.
GRASP_OFFSET_M = 0.08
#: Bar centre during the handover (metres, tabletop frame); the bar lies along x there.
HANDOVER_XYZ = (0.0, 0.0, 0.14)
#: Ready pose: raised, gripper pointing down at the table in front of the arm.
READY = (0.0, -0.8, 0.5, 0.0, 1.871, 0.0)
_VX300S = ("waist", "shoulder", "elbow", "forearm_roll", "wrist_angle", "wrist_rotate")


def _seeds(bearing: float, roll: float) -> list[Any]:
    return [
        np.array([bearing, shoulder, elbow, 0.0, wrist, arm.wrap(math.pi / 2 - roll)])
        for shoulder, elbow, wrist in ((-0.3, 0.6, 1.2), (0.1, 0.1, 1.4), (-0.6, 1.0, 1.0))
    ]


def _rig(side: str, x: float, yaw_deg: float) -> ArmRig:
    gripper = ALOHA.left_gripper if side == "left" else ALOHA.right_gripper
    return ArmRig(
        name=f"aloha_{side}",
        robot=SingleArm(
            f"aloha_{side}",
            model=ALOHA.model,
            arm=ALOHA.left if side == "left" else ALOHA.right,
            gripper=gripper,
        ),
        assets=ALOHA_MODEL,
        case_set=CASE_SET,
        cases_file=CASES_FILE,
        root_body=f"{side}/base_link",
        base_xy=(x, -0.019),
        base_yaw_deg=yaw_deg,
        base_z_m=0.02,
        arm_actuators=tuple(f"{side}/{j}" for j in _VX300S),
        gripper_actuator=f"{side}/gripper",
        gripper_joint=f"{side}/left_finger",
        grip_open=0.037,
        grip_closed=0.002,
        closed_below=0.02,
        opening_above=0.01,
        ready=READY,
        site=f"{side}/tcp",
        site_body=f"{side}/gripper_link",
        site_pos=(0.13, 0.0, 0.0),
        site_quat=(math.sqrt(0.5), -math.sqrt(0.5), 0.0, 0.0),
        pad_bodies=(f"{side}/left_finger_link", f"{side}/right_finger_link"),
        pad_priority=None,
        fixed_jaw_offset_m=0.0,
        grasp_gap_m=0.0,
        min_site_z_m=0.018,
        seeds=_seeds,
        roll_index=5,
        pan_index=0,
        gravity_compensation=True,
        pedestal_radius_m=0.05,
        protocol=HANDOVER_PROTOCOL,
        camera=(0.0, 0.0, 0.12, 1.05, -90.0, -30.0),
    )


GIVER = _rig("left", -0.469, 0.0)
TAKER = _rig("right", 0.469, 180.0)


def _toward_giver(yaw: float) -> float:
    """Sign of the grasp offset along the bar (at ``yaw``) that lies on the giver's (-x) side."""
    return -1.0 if math.cos(yaw) >= 0 else 1.0


class AlohaWorld(SimWorld):
    """Physics behind the five ``handover-v1`` tools: the giver picks, hands over, the taker
    places."""

    TOOLS = HANDOVER_TOOLS
    ACTUATING = ACTUATING_TOOLS | {"handover"}
    AUDITED = ("pick", "handover", "place", "abort")

    def __init__(
        self,
        case: FixtureCase,
        clock: FixtureClock,
        model: Any,
        frame_hook: FrameHook | None = None,
    ) -> None:
        self.holder: str | None = None
        self.handovers = 0
        self._giver_grasp: arm.Grasp | None = None
        #: Grasp point offset along the bar, in the bar's own frame, of the arm holding it.
        self._offset = 0.0
        super().__init__(case, clock, model, frame_hook, GIVER, (TAKER,))
        self.giver, self.taker = self.arms

    def handlers(self) -> dict[str, Any]:
        return {**super().handlers(), "handover": self.handover}

    # ---------------------------------------------------------------------- observations
    def _holding_arm(self) -> ArmState | None:
        if self.holder is None:
            return None
        return self.giver if self.holder == "giver" else self.taker

    def _still_holding(self) -> bool:
        holding = self._holding_arm()
        still = holding is not None and self._gripper_holding(holding)
        if not still:
            self.holder = None
        return still

    def _observation_extra(self) -> dict[str, Any]:
        return {"holder": self.holder if self.held else None}

    # ------------------------------------------------------------------------------ tools
    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "yaw_deg", "grasp_height_m"))
        if reason is None and not all(abs(v) <= TABLE_BOUND_M for v in self.box_xy()):
            reason = "unreachable_target"
        if reason is None and self.held:
            reason = "already_held"
        grasp: arm.Grasp | None = None
        offset = 0.0
        if reason is None:
            yaw = math.radians(args["yaw_deg"])
            offset = _toward_giver(yaw) * GRASP_OFFSET_M
            x = args["x_m"] + offset * math.cos(yaw)
            y = args["y_m"] + offset * math.sin(yaw)
            # Jaws across the bar, turned so that at the meeting point the wrist camera mount
            # faces away from the taker (jaw yaw -90 degrees there).
            quarter = 3 if offset < 0 else 1
            grasp = arm.plan_grasp(
                self.giver.kin, x, y, args["yaw_deg"], args["grasp_height_m"], None, (quarter,)
            )
            if grasp is None:
                reason = "unreachable_target"
        entry = self._log("pick", reason, context)
        if reason is not None or grasp is None:
            return self._reply("rejected", reason=reason)
        self.picks += 1
        before = self.box_pose()
        fumble = self.picks <= self.case.misgrasp_picks
        giver = self.giver
        self._move(giver.kin.ready_toward(grasp.hover), APPROACH_S, giver.rig.grip_open, giver)
        self._move(grasp.hover, APPROACH_S, arm=giver)
        if not fumble:
            self._move(grasp.grasp, DESCEND_S, arm=giver)
        self._move(grasp.hover if fumble else grasp.grasp, CLOSE_S, giver.rig.grip_closed, giver)
        self._hold(GRIP_SETTLE_S)
        self._move(grasp.carry, LIFT_S, arm=giver)
        entry["effect"] = self.lifted or self._displaced(before)
        if self._gripper_holding(giver):
            self.held = True
            self.holder = "giver"
            self._giver_grasp = grasp
            self._offset = offset
            self._last_result = "held"
            return self._reply("ok", held=True)
        self._move(giver.kin.ready_toward(grasp.hover), RETREAT_S, giver.rig.grip_open, giver)
        self._last_result = "misgrasp"
        return self._reply("error", held=False, reason="misgrasp")

    def handover(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ())
        if reason is None and (not self.held or self.holder != "giver"):
            reason = "not_held"
        meeting: arm.Placement | None = None
        taking: arm.Grasp | None = None
        flip = 0.0
        if reason is None and self._giver_grasp is not None:
            # The bar lies along x at the meeting point, the giver's end toward the giver.
            flip = 0.0 if self._offset < 0 else math.pi
            hx, hy, hz = HANDOVER_XYZ
            meeting = arm.plan_place(
                self.giver.kin,
                self._giver_grasp,
                hx - GRASP_OFFSET_M,
                hy,
                hz,
                math.degrees(flip),
            )
            # The taker's jaw yaw is +90 degrees: its camera mount faces away from the giver.
            taking = arm.plan_grasp(
                self.taker.kin,
                hx + GRASP_OFFSET_M,
                hy,
                math.degrees(flip),
                hz,
                None,
                (1 if flip == 0.0 else 3,),
            )
            if meeting is None or taking is None:
                reason = "unreachable_target"
        entry = self._log("handover", reason, context)
        if reason is not None or meeting is None or taking is None:
            return self._reply("rejected", reason=reason)
        self.handovers += 1
        before = self.box_pose()
        giver, taker = self.giver, self.taker
        assert self._giver_grasp is not None
        self._move(meeting.approach, CARRY_S, arm=giver)
        self._move(meeting.release, DESCEND_S, arm=giver)
        drop = self.handovers <= self.case.dropped_handovers
        if not drop:
            self._move(taker.kin.ready_toward(taking.hover), APPROACH_S, taker.rig.grip_open, taker)
            self._move(taking.hover, APPROACH_S, arm=taker)
            self._move(taking.grasp, DESCEND_S, arm=taker)
            self._move(taking.grasp, CLOSE_S, taker.rig.grip_closed, taker)
            self._hold(GRIP_SETTLE_S)
        self._move(meeting.release, RELEASE_S, giver.rig.grip_open, giver)
        self._move(meeting.approach, RETREAT_S, arm=giver)
        self._move(giver.kin.ready_toward(meeting.approach), RETREAT_S, arm=giver)
        self._giver_grasp = None
        entry["effect"] = self._displaced(before) or self.lifted
        if not drop and self._gripper_holding(taker):
            self.holder = "taker"
            # The taker holds the bar GRASP_OFFSET_M from its centre, toward +x at the meeting.
            self._offset = GRASP_OFFSET_M if flip == 0.0 else -GRASP_OFFSET_M
            self.grasp = taking
            self._last_result = "handed_over"
            return self._reply("ok", holder="taker")
        self.held = False
        self.holder = None
        self._hold(RELEASE_S)
        self._last_result = "dropped"
        return self._reply("error", holder=None, reason="dropped")

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
        if reason is None and not (
            abs(args["x_m"]) <= TABLE_BOUND_M and abs(args["y_m"]) <= TABLE_BOUND_M
        ):
            reason = "unreachable_target"
        if reason is None and (not self.held or self.holder != "taker" or self.grasp is None):
            reason = "not_held"
        placement: arm.Placement | None = None
        if reason is None and self.grasp is not None:
            yaw = math.radians(args["yaw_deg"])
            placement = arm.plan_place(
                self.taker.kin,
                self.grasp,
                args["x_m"] + self._offset * math.cos(yaw),
                args["y_m"] + self._offset * math.sin(yaw),
                args["z_m"],
                args["yaw_deg"],
            )
            if placement is None:
                reason = "unreachable_target"
        entry = self._log("place", reason, context)
        if reason is not None or placement is None:
            return self._reply("rejected", reason=reason)
        before = self.box_pose()
        taker = self.taker
        assert self.grasp is not None
        self._move(self.grasp.carry, LIFT_S, arm=taker)
        self._move(placement.approach, CARRY_S, arm=taker)
        self._move(placement.release, DESCEND_S, arm=taker)
        self._move(placement.release, RELEASE_S, taker.rig.grip_open, taker)
        self.held = False
        self.holder = None
        self.grasp = None
        self.released_ms = self.clock()
        self._hold(0.2)
        self._move(placement.approach, RETREAT_S, arm=taker)
        entry["effect"] = self._displaced(before)
        self._last_result = "released"
        return self._reply("ok", released=True)

    def abort(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._reply("rejected", reason="malformed_arguments")
        for each in self.arms:
            self.data.ctrl[each.joints] = self.data.qpos[each.kin.qpos]
        return super().abort(args)


def new_aloha_world(
    case: FixtureCase, clock: FixtureClock, assets: Path, frame_hook: FrameHook | None = None
) -> AlohaWorld:
    """Build the two-arm scene with the bar for ``case`` and wrap it in an ``AlohaWorld``."""
    model = build_model(case, assets, GIVER, others=(TAKER,), bar_length_m=BAR_LENGTH_M)
    return AlohaWorld(case, clock, model, frame_hook)


def load_aloha_cases(path: Path = CASES_FILE) -> tuple[FixtureCase, ...]:
    """The frozen ALOHA case set (``uzumaki.baseline-cases/1.0``, protocol ``handover-v1``)."""
    return load_cases(path, HANDOVER_PROTOCOL)


def aloha_environment(
    asset_dir: Path, frame_hook: FrameHook | None = None
) -> Callable[[FixtureCase], CaseEnvironment]:
    """Environment factory for ``run_case``: a fresh two-arm world and evaluator per case."""

    def build(case: FixtureCase) -> CaseEnvironment:
        clock = FixtureClock()
        world = new_aloha_world(case, clock, asset_dir, frame_hook)
        return CaseEnvironment(clock, world, SimEvaluator(world))

    return build


#: Seed recipe ranges (SHA-256 as in ``pick-place-v1``): bar cross-section m, centre x/y m and
#: yaw degrees. The bar starts on the giver's side and its target is on the taker's side.
RANGES = {
    "size": (0.025, 0.04),
    "initial_x": (-0.11, -0.07),
    "initial_y": (-0.08, 0.08),
    "target_x": (0.07, 0.11),
    "target_y": (-0.08, 0.08),
    "yaw": (-30.0, 30.0),
}


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
    pick, handover, place = admitted
    return {
        "primary": primary,
        "progress": progress,
        "state": state,
        "status": status,
        "attempts": attempts,
        "admitted": {"pick": pick, "handover": handover, "place": place},
    }


#: The eight fault cases: the SO-101's, with ``dropped-handover`` in place of
#: ``stale-after-pick`` (call numbers: observe 1, pick 2, observe 3, handover 4, observe 5, ...).
FAULT_CASES: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    ("nominal", {}, _expected("completed", "goal_reached", "completed", 1, 1, 1, 1)),
    ("unreachable-goal", {}, _expected("unreachable_target", "partial", "blocked", 1, 1, 1, 0)),
    (
        "failed-grasp",
        {"misgrasp_picks": 1},
        _expected("completed", "goal_reached", "completed", 2, 2, 1, 1),
    ),
    (
        "dropped-handover",
        {"dropped_handovers": 1},
        _expected("completed", "goal_reached", "completed", 2, 2, 2, 1),
    ),
    (
        "stale-observation",
        {"stale_observes": [1]},
        _expected("completed", "goal_reached", "completed", 2, 1, 1, 1),
    ),
    (
        "retry-exhausted",
        {"misgrasp_picks": 3},
        _expected("budget_exhausted", "none", "budget_exhausted", 3, 3, 0, 0),
    ),
    (
        "cancellation",
        {"withdraw_goal_after_call": 2},
        _expected("cancelled", "partial", "cancelled", 1, 1, 0, 0),
    ),
    (
        "worker-crash",
        {"crash_on_call": 3},
        _expected("evaluation_failure", "partial", "failed", 1, 1, 0, 0),
    ),
)


def build_case_set() -> dict[str, Any]:
    """The ALOHA case set: eight fault cases on seeds ``aloha-1001`` to ``1008`` and twenty
    nominal cases on ``aloha-1009`` to ``1028`` (``uzumaki.baseline-cases/1.0``)."""
    cases = []
    for index in range(28):
        geometry = f"aloha-dev-{index + 1:03d}"
        values = generate_geometry(f"aloha-{1001 + index}")
        if index < len(FAULT_CASES):
            case_id, faults, expected = FAULT_CASES[index]
        else:
            case_id, faults, expected = geometry, {}, FAULT_CASES[0][2]
        if case_id == "unreachable-goal":
            values["target_xy_m"] = [0.4, 0.0]
            geometry += "-target-0.40"
        cases.append(
            {
                "case_id": case_id,
                "geometry": geometry,
                **values,
                "faults": {**_NO_FAULTS, **faults},
                "expected": copy.deepcopy(expected),
            }
        )
    return {"format": "uzumaki.baseline-cases/1.0", "protocol": HANDOVER_PROTOCOL, "cases": cases}
