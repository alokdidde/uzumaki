"""Scripted fixture world for ``handover-v1``: the offline stand-in for a two-arm simulator.

``HandoverWorld`` extends ``FixtureWorld`` with the roles of the ``handover`` task pack. ``pick``
grasps with the giver; ``handover`` passes the held object to the taker; ``place`` is admitted
only while the taker holds it. Observations add ``holder``: ``"giver"``, ``"taker"`` or ``None``.
A case's ``dropped_handovers`` fault makes that many handovers drop the object: it lands on the
tabletop midway between the arms, and the next attempt must pick it up again. Admission order,
the log, the audit and the evaluator are the fixture's, with ``handover`` counted as actuation.
"""

from __future__ import annotations

from typing import Any

from uzumaki.baseline import CaseEnvironment
from uzumaki.broker import Handler
from uzumaki.fixture_env import (
    ACTUATING_TOOLS,
    HANDOVER_TOOLS,
    FixtureCase,
    FixtureClock,
    FixtureEvaluator,
    FixtureWorld,
)

HANDOVER_MS = 1500
#: Where a dropped object lands (tabletop x, metres): midway between the giver and the taker.
DROP_X_M = 0.0


class HandoverWorld(FixtureWorld):
    """Fixture world with a giver and a taker (``handover-v1``)."""

    TOOLS = HANDOVER_TOOLS
    ACTUATING = ACTUATING_TOOLS | {"handover"}
    AUDITED = ("pick", "handover", "place", "abort")

    def __init__(self, case: FixtureCase, clock: FixtureClock) -> None:
        super().__init__(case, clock)
        self.holder: str | None = None
        self.handovers = 0

    def handlers(self) -> dict[str, Handler]:
        return {**super().handlers(), "handover": self.handover}

    def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        reply = super().observe(args)
        if reply["status"] == "ok":
            reply["holder"] = self.holder if self.held else None
        return reply

    def pick(self, args: dict[str, Any]) -> dict[str, Any]:
        reply = super().pick(args)
        if self.held:
            self.holder = "giver"
        return reply

    def handover(self, args: dict[str, Any]) -> dict[str, Any]:
        reason, context = self._admission(args, ())
        if reason is None and (not self.held or self.holder != "giver"):
            reason = "not_held"
        entry = self._log("handover", reason, context)
        if reason is not None:
            return self._reply("rejected", reason=reason)
        self.clock.advance(HANDOVER_MS)
        self.handovers += 1
        entry["effect"] = True
        if self.handovers <= self.case.dropped_handovers:
            self.held = False
            self.holder = None
            self.box = {"x_m": DROP_X_M, "y_m": self.box["y_m"], "yaw_deg": self.box["yaw_deg"]}
            self._last_result = "dropped"
            return self._reply("error", holder=None, reason="dropped")
        self.holder = "taker"
        self._last_result = "handed_over"
        return self._reply("ok", holder="taker")

    def place(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.held and self.holder != "taker":
            reason, context = self._admission(args, ("x_m", "y_m", "z_m", "yaw_deg"))
            self._log("place", reason or "not_held", context)
            return self._reply("rejected", reason=reason or "not_held")
        reply = super().place(args)
        if not self.held:
            self.holder = None
        return reply


def handover_environment(case: FixtureCase) -> CaseEnvironment:
    """A fresh scripted handover world and the fixture evaluator for one case."""
    clock = FixtureClock()
    world = HandoverWorld(case, clock)
    return CaseEnvironment(clock, world, FixtureEvaluator(world))
