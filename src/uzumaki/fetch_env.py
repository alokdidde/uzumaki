"""Scripted fixture world for ``fetch-v1``: the offline stand-in for a mobile manipulator.

The room has two tables of the ``pick-place-v1`` size, ``TABLES`` (centres in the room frame,
tops at z = 0). A case's object starts on table ``a`` and its target is on table ``b``, too far
apart to reach both from one place. ``FetchWorld`` extends ``FixtureWorld`` with ``go_to``: it
moves the body to the work station in front of the table under a given point (or rejects a
point that is on no table as ``unreachable_target``). ``pick`` and ``place`` are admitted only
for a pose on the table the body stands at. Observations add ``base_pose`` (room frame) and
``within_reach``: whether the object and the target can be reached from where the body stands.

A case's ``stopped_short_drives`` fault makes that many ``go_to`` moves stop halfway (an error,
``stopped_short``): the body stands at no table and the next attempt has to move again. Admission
order, the log, the audit and the evaluator are the fixture's, with ``go_to`` counted as
actuation. Every value here is a fixture constant, not a measurement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from uzumaki.baseline import CaseEnvironment
from uzumaki.broker import Handler
from uzumaki.fixture_env import (
    ACTUATING_TOOLS,
    FETCH_TOOLS,
    GRASP_TOLERANCE_M,
    TABLE_BOUND_M,
    TOOL_MS,
    FixtureCase,
    FixtureClock,
    FixtureEvaluator,
    FixtureWorld,
)

#: Table name -> centre (x, y) in the room frame, metres. Each table is the protocol's
#: 0.5 x 0.5 m tabletop and is worked from its -y edge. Table ``a`` is the protocol's tabletop
#: frame, so initial poses keep its bounds; targets on ``b`` stay within the 1 m target bound.
TABLES: Mapping[str, tuple[float, float]] = MappingProxyType({"a": (0.0, 0.0), "b": (0.8, 0.0)})
#: Fixture work station: the body's centre this far from the table's -y edge, facing +y.
STANDOFF_M = 0.27
#: Where the body starts: between the tables, at no station.
START_POSE = (0.4, -(TABLE_BOUND_M + STANDOFF_M), 90.0)
GO_TO_MS = 3000


def table_under(x: float, y: float) -> str | None:
    """The table whose top contains (x, y), or ``None``."""
    for name, (cx, cy) in TABLES.items():
        if abs(x - cx) <= TABLE_BOUND_M and abs(y - cy) <= TABLE_BOUND_M:
            return name
    return None


def station(x: float, y: float) -> tuple[float, float, float] | None:
    """Fixture base pose (x, y, yaw degrees) to work at (x, y), or ``None`` off every table."""
    table = table_under(x, y)
    if table is None:
        return None
    return x, TABLES[table][1] - TABLE_BOUND_M - STANDOFF_M, 90.0


def _base_pose(pose: tuple[float, float, float]) -> dict[str, float]:
    x, y, yaw = pose
    return {"x_m": round(x, 6), "y_m": round(y, 6), "yaw_deg": round(yaw, 6)}


class FetchWorld(FixtureWorld):
    """Fixture world with a body that moves between two tables (``fetch-v1``)."""

    TOOLS = FETCH_TOOLS
    ACTUATING = ACTUATING_TOOLS | {"go_to"}
    AUDITED = ("pick", "go_to", "place", "abort")

    def __init__(self, case: FixtureCase, clock: FixtureClock) -> None:
        super().__init__(case, clock)
        self.base = START_POSE
        #: The table the body stands at, or ``None``.
        self.at: str | None = None
        self.drives = 0

    def handlers(self) -> dict[str, Handler]:
        return {**super().handlers(), "go_to": self.go_to}

    def reachable(self, x: float, y: float) -> bool:
        return self.at is not None and table_under(x, y) == self.at

    def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        reply = super().observe(args)
        if reply["status"] == "ok":
            estimate, target = reply["object_pose_estimate"], reply["target_pose"]
            reply["base_pose"] = _base_pose(self.base)
            reply["within_reach"] = {
                "object": self.reachable(estimate["x_m"], estimate["y_m"]),
                "target": self.reachable(target["x_m"], target["y_m"]),
            }
        return reply

    def go_to(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m"))
        goal = None if reason is not None else station(args["x_m"], args["y_m"])
        if reason is None and goal is None:
            reason = "unreachable_target"
        entry = self._log("go_to", reason, context)
        if reason is not None or goal is None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(GO_TO_MS)
        self.drives += 1
        entry["effect"] = True
        if self.drives <= self.case.stopped_short_drives:
            sx, sy, syaw = self.base
            self.base = ((sx + goal[0]) / 2, (sy + goal[1]) / 2, syaw)
            self.at = None
            self._last_result = "stopped_short"
            return self._reply("error", base_pose=_base_pose(self.base), reason="stopped_short")
        self.base = goal
        self.at = table_under(args["x_m"], args["y_m"])
        self._last_result = "arrived"
        return self._reply("ok", base_pose=_base_pose(self.base))

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "yaw_deg", "grasp_height_m"))
        if reason is None and not self.reachable(self.box["x_m"], self.box["y_m"]):
            reason = "unreachable_target"
        if reason is None and self.held:
            reason = "already_held"
        entry = self._log("pick", reason, context)
        if reason is not None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(TOOL_MS["pick"])
        self.picks += 1
        offset = math.hypot(args["x_m"] - self.box["x_m"], args["y_m"] - self.box["y_m"])
        if self.picks <= self.case.misgrasp_picks or offset > GRASP_TOLERANCE_M:
            self._last_result = "misgrasp"
            return self._reply("error", held=False, reason="misgrasp")
        self.held = True
        self.lifted = True
        entry["effect"] = True
        self._last_result = "held"
        return self._reply("ok", held=True)

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
        if reason is None and not self.reachable(args["x_m"], args["y_m"]):
            reason = "unreachable_target"
        if reason is None and not self.held:
            reason = "not_held"
        entry = self._log("place", reason, context)
        if reason is not None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(TOOL_MS["place"])
        self.box = {"x_m": args["x_m"], "y_m": args["y_m"], "yaw_deg": args["yaw_deg"]}
        self.held = False
        self.released_ms = self.clock()
        entry["effect"] = True
        self._last_result = "released"
        return self._reply("ok", released=True)


def fetch_environment(case: FixtureCase) -> CaseEnvironment:
    """A fresh scripted fetch world and the fixture evaluator for one case."""
    clock = FixtureClock()
    world = FetchWorld(case, clock)
    return CaseEnvironment(clock, world, FixtureEvaluator(world))
