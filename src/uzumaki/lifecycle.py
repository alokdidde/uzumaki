"""Run lifecycle state machine (contract 1.0 ``lifecycle``) with idempotent terminal transitions.

States, edges and terminal immutability follow ``docs/contracts/v1.json``: ``queued``,
``running``, ``cancelling`` and terminal ``completed``, ``cancelled``, ``failed``, ``rejected``.
Nothing leaves a terminal state. Repeating the transition that produced the current state is a
recorded no-op (``terminal_repeat_ignored`` once terminal); any other disallowed request is
recorded as ``lifecycle_violation`` and changes nothing. Requests never raise for these cases, so
racing callers (a deadline, a cancel and a completion) can all ask safely and the first wins.

A terminal run also carries a harness *status* (why the run stopped) and its observed *progress*.
The status is not a task-success claim: the independent evaluator owns outcomes. Progress only
increases; cancellation and failure never report effects as rolled back.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

STATES = ("queued", "running", "cancelling", "completed", "cancelled", "failed", "rejected")
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "rejected"})

# Contract 1.0 lifecycle.transitions: (from, to) -> triggering event.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("queued", "running"): "admitted_and_reset_ready",
    ("queued", "rejected"): "run_admission_rejected",
    ("queued", "cancelled"): "cancel_before_start",
    ("queued", "failed"): "reset_or_readiness_failure",
    ("running", "completed"): "evaluator_terminal_event",
    ("running", "cancelling"): "cancel_requested",
    ("running", "failed"): "component_crash_or_invalid_evidence",
    ("cancelling", "cancelled"): "safe_stop_reported_and_evaluated",
    ("cancelling", "failed"): "component_crash_or_invalid_evidence",
}

PROGRESS = ("none", "partial", "goal_reached")

# Harness termination status -> contract lifecycle state. The lifecycle state ``completed`` means
# the run ended with a terminal case event, not that the task succeeded.
STATUS_STATES: dict[str, str] = {
    "completed": "completed",
    "abstained": "completed",
    "blocked": "completed",
    "partial_progress": "completed",
    "timeout": "completed",
    "budget_exhausted": "completed",
    "cancelled": "cancelled",
    "failed": "failed",
    "rejected": "rejected",
}
# Statuses that harness logic may request when it stops voluntarily.
LOGIC_STATUSES = ("completed", "abstained", "blocked", "partial_progress")
# Progress each status requires; other statuses accept whatever progress was observed.
_REQUIRED_PROGRESS = {
    "completed": "goal_reached",
    "partial_progress": "partial",
    "rejected": "none",
}


class LifecycleError(ValueError):
    """A request named an unknown status or progress value (a caller bug, not a race)."""


@dataclass(frozen=True)
class Transition:
    """Result of a lifecycle request. ``diagnostic`` is set when the request changed nothing."""

    applied: bool
    state: str
    diagnostic: str | None = None


def _ms(clock: Callable[[], int]) -> int:
    now = clock()
    if type(now) is not int or now < 0:
        raise LifecycleError("clock returned an invalid time")
    return now


class Lifecycle:
    """Thread-safe lifecycle of one run. ``lock`` may be shared by an owning supervisor."""

    def __init__(self, clock: Callable[[], int]) -> None:
        self.lock = threading.RLock()
        self._clock = clock
        self._state = "queued"
        self._status: str | None = None
        self._detail: str | None = None
        self._progress = "none"
        self._last_request: tuple[str, str] | None = None
        self.history: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.post_terminal: list[dict[str, Any]] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def status(self) -> str | None:
        return self._status

    @property
    def detail(self) -> str | None:
        return self._detail

    @property
    def progress(self) -> str:
        return self._progress

    @property
    def terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def diagnose(self, kind: str, **fields: Any) -> None:
        with self.lock:
            self.diagnostics.append({"kind": kind, "state": self._state, **fields})

    def _refuse(self, request: tuple[str, str]) -> Transition:
        if self.terminal and request[1] == self._status:
            kind = "terminal_repeat_ignored"
        elif not self.terminal and request == self._last_request:
            kind = "repeat_ignored"
        else:
            kind = "lifecycle_violation"
        self.diagnose(kind, request=request[0], value=request[1])
        return Transition(False, self._state, kind)

    def _move(self, to: str, request: tuple[str, str], status: str | None, detail: str) -> None:
        on = TRANSITIONS[(self._state, to)]
        self.history.append(
            {"from": self._state, "to": to, "on": on, "detail": detail, "at_ms": _ms(self._clock)}
        )
        self._state = to
        self._last_request = request
        if to in TERMINAL_STATES:
            self._status = status
            self._detail = detail

    def start(self) -> Transition:
        """``queued`` -> ``running`` once the run is admitted and ready."""
        with self.lock:
            request = ("start", "running")
            if self._state != "queued":
                return self._refuse(request)
            self._move("running", request, None, "admitted")
            return Transition(True, self._state)

    def cancel(self, detail: str = "cancel_requested") -> Transition:
        """Cancel before start (-> ``cancelled``) or while running (-> ``cancelling``)."""
        with self.lock:
            request = ("cancel", "cancelled")
            if self._state == "queued":
                self._move("cancelled", request, "cancelled", detail)
                return Transition(True, self._state)
            if self._state == "running":
                self._move("cancelling", request, None, detail)
                return Transition(True, self._state)
            return self._refuse(request)

    def finish(self, status: str, detail: str = "") -> Transition:
        """Request a terminal status. The first terminal request wins; later ones change nothing.

        ``cancelled`` finishes a ``cancelling`` run. ``rejected`` and ``failed`` may end a
        ``queued`` run. While ``cancelling``, only ``cancelled`` and ``failed`` are allowed.
        """
        if status not in STATUS_STATES:
            raise LifecycleError("unknown termination status")
        with self.lock:
            request = ("finish", status)
            to = STATUS_STATES[status]
            if (self._state, to) not in TRANSITIONS or (
                status == "cancelled" and self._state != "cancelling"
            ):
                return self._refuse(request)
            required = _REQUIRED_PROGRESS.get(status)
            if required is not None and self._progress != required:
                return self._refuse(request)
            self._move(to, request, status, detail or status)
            return Transition(True, self._state)

    def record_progress(self, progress: str, source: str = "environment") -> None:
        """Record independently observed progress. It never decreases.

        After a terminal transition the outcome is not rewritten; the observation is kept as
        separate post-terminal evidence instead.
        """
        if progress not in PROGRESS:
            raise LifecycleError("unknown progress value")
        with self.lock:
            if self.terminal:
                self.post_terminal.append(
                    {"progress": progress, "source": source, "at_ms": _ms(self._clock)}
                )
                return
            if PROGRESS.index(progress) > PROGRESS.index(self._progress):
                self._progress = progress
