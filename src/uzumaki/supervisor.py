"""Bounded harness run: budgets, lifecycle, cancellation and late-result fencing around calls.

``BoundedRun`` sits between deterministic harness logic and its model provider and tool executor.
Every call is issued a ``Ticket`` (run ID, attempt ID, call ID and deadline) and every result is
settled against it. A result is applied only while the run is ``running``, for the active attempt,
for a still-open call, before that call's deadline. Anything else is fenced: recorded by digest
(effects are retained as evidence, never undone) and never returned to the logic.

Before each call the run checks its state, wall time and budgets, so once it leaves ``running``
no further provider or tool call is made. Cancellation takes effect at the next call boundary; an
in-flight call is bounded by its own deadline and its result is fenced. Resources registered with
``add_resource`` are released exactly once when the run reaches a terminal state.

The clock is injectable (integer milliseconds) so logic tests use a fake clock. A plain provider
cannot be preempted: its result is checked against the deadline when it returns. A provider that
implements ``request_within`` (for example ``uzumaki.deadline.ProcessProvider``) is given the
remaining time and enforces it itself.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from uzumaki.budgets import BudgetError, Budgets, Ledger, Usage, parse_usage
from uzumaki.evidence import (
    ID_PATTERN,
    TOOL_RESULT_STATUSES,
    EvidenceError,
    ToolExecutor,
    ToolResult,
    canonical_json,
    sha256_of,
)
from uzumaki.faults import ProviderTimeout, TransientProviderError, WorkerFailure
from uzumaki.lifecycle import LOGIC_STATUSES, Lifecycle, Transition


class ModelProvider(Protocol):
    """The issue-03 provider interface, e.g. ``uzumaki.providers.RecordedProvider``."""

    def request(self, request: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class DeadlineProvider(Protocol):
    """A provider that can enforce a deadline itself (and release its resources on expiry)."""

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]: ...


class RunStopped(Exception):
    """The run is no longer ``running``; the harness logic must stop making calls."""

    def __init__(self, state: str, status: str | None, detail: str | None) -> None:
        super().__init__(f"run stopped in state {state}")
        self.state = state
        self.status = status
        self.detail = detail


class ResultFenced(Exception):
    """A result arrived for a call that is no longer current while the run is still running."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"result fenced: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Ticket:
    run_id: str
    attempt_id: str
    call_id: str
    kind: str
    deadline_ms: int
    timeout_ms: int


@dataclass(frozen=True)
class Settlement:
    accepted: bool
    reason: str | None


@dataclass(frozen=True)
class RunReport:
    run_id: str
    state: str
    status: str | None
    detail: str | None
    progress: str
    attempt_id: str | None
    usage: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    fenced: list[dict[str, Any]] = field(default_factory=list)
    post_terminal: list[dict[str, Any]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _size(value: Any) -> int | None:
    try:
        return len(canonical_json(value))
    except EvidenceError:
        return None


def _digest(value: Any) -> str | None:
    try:
        return sha256_of(value)
    except EvidenceError:
        return None


class BoundedRun:
    """One harness run with enforced budgets, lifecycle and fencing. Thread-safe."""

    def __init__(
        self,
        *,
        run_id: str,
        budgets: Budgets,
        provider: ModelProvider | None = None,
        tools: ToolExecutor | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not ID_PATTERN.match(run_id):
            raise ValueError("run_id is not a valid identifier")
        if not isinstance(budgets, Budgets):
            raise BudgetError("budgets must be a Budgets instance")
        self.run_id = run_id
        self.budgets = budgets
        self._provider = provider
        self._tools = tools
        self._clock = clock or _monotonic_ms
        self._life = Lifecycle(self._clock)
        self._lock = self._life.lock
        self._ledger = Ledger(budgets)
        self._started_ms: int | None = None
        self._attempt_id: str | None = None
        self._seq = 0
        self._open: dict[str, Ticket] = {}
        self._abandoned: set[str] = set()
        self._settled: set[str] = set()
        self._fenced: list[dict[str, Any]] = []
        self._resources: list[tuple[str, Callable[[], None]]] = []
        self._released: list[str] = []
        self._release_started = False

    # ----------------------------------------------------------------- state and lifecycle
    @property
    def state(self) -> str:
        return self._life.state

    @property
    def status(self) -> str | None:
        return self._life.status

    @property
    def progress(self) -> str:
        return self._life.progress

    @property
    def attempt_id(self) -> str | None:
        return self._attempt_id

    @contextmanager
    def _guarded(self) -> Iterator[None]:
        try:
            with self._lock:
                yield
        finally:
            self._release_if_terminal()

    def start(self) -> Transition:
        """``queued`` -> ``running``; starts the wall clock and opens attempt ``a1``."""
        with self._guarded():
            transition = self._life.start()
            if transition.applied:
                self._started_ms = self._now()
                self._open_attempt()
            return transition

    def reject(self, detail: str = "run_admission_rejected") -> Transition:
        with self._guarded():
            return self._life.finish("rejected", detail)

    def cancel(self, detail: str = "cancel_requested") -> Transition:
        """Request cancellation. Safe to call from any thread, any number of times."""
        with self._guarded():
            return self._life.cancel(detail)

    def complete_cancellation(self, detail: str = "safe_stop_reported") -> Transition:
        """``cancelling`` -> ``cancelled`` once in-flight work has stopped."""
        with self._guarded():
            return self._life.finish("cancelled", detail)

    def finish(self, status: str, detail: str = "") -> Transition:
        """Voluntary stop by harness logic: ``completed``, ``abstained``, ``blocked`` or
        ``partial_progress``. Refused (and recorded) if the run already stopped or is cancelling.
        """
        if status not in LOGIC_STATUSES:
            raise ValueError("harness logic may only finish with a voluntary status")
        with self._guarded():
            return self._life.finish(status, detail)

    def fail(self, detail: str) -> Transition:
        with self._guarded():
            return self._life.finish("failed", detail)

    def record_progress(self, progress: str, source: str = "environment") -> None:
        """Record independently observed progress; kept as post-terminal evidence once stopped."""
        self._life.record_progress(progress, source)

    def add_resource(self, name: str, release: Callable[[], None]) -> None:
        """Register a release callback, run once (last registered first) at terminal state.

        A resource added after the run stopped is released immediately.
        """
        with self._lock:
            self._resources.append((name, release))
            late = self._release_started
        if late:
            self._release_resources()
        else:
            self._release_if_terminal()

    def new_attempt(self) -> str:
        """Close the active attempt (its open calls are abandoned) and open the next one."""
        with self._guarded():
            self._require_running()
            reason = self._ledger.before_attempt()
            if reason is not None:
                self._terminate("budget_exhausted", reason)
            self._abandon_open()
            return self._open_attempt()

    def checkpoint(self) -> None:
        """Raise ``RunStopped`` if the run was cancelled, stopped, or ran out of wall time."""
        with self._guarded():
            self._require_running()

    def report(self) -> RunReport:
        with self._lock:
            return RunReport(
                run_id=self.run_id,
                state=self._life.state,
                status=self._life.status,
                detail=self._life.detail,
                progress=self._life.progress,
                attempt_id=self._attempt_id,
                usage=self._ledger.snapshot(),
                history=copy.deepcopy(self._life.history),
                diagnostics=copy.deepcopy(self._life.diagnostics),
                fenced=copy.deepcopy(self._fenced),
                post_terminal=copy.deepcopy(self._life.post_terminal),
                released=list(self._released),
            )

    def execute(self, logic: Callable[[BoundedRun], str]) -> RunReport:
        """Start, run ``logic`` and always reach a terminal state; return the report.

        ``logic`` returns a voluntary status. ``RunStopped`` ends it quietly; any other exception
        fails the run. A cancelled run is finalized as ``cancelled`` after the logic returns.
        """
        if self.start().applied:
            try:
                status = logic(self)
            except RunStopped:
                pass
            except Exception:
                self.fail("harness_logic_error")
            except BaseException:
                self.fail("interrupted")  # still terminal and released before propagating
                raise
            else:
                if status in LOGIC_STATUSES:
                    self.finish(status)
                else:
                    self.fail("invalid_logic_status")
        with self._guarded():
            if self._life.state == "cancelling":
                self._life.finish("cancelled", "safe_stop_reported")
            elif self._life.state == "running":
                self._life.finish("failed", "run_not_terminated")
        return self.report()

    # ----------------------------------------------------------------------------- calls
    def ticket(self, kind: str) -> Ticket:
        """Issue a ticket for one ``model`` or ``tool`` call, consuming its budget."""
        with self._guarded():
            return self._ticket(kind)

    def submit(self, ticket: Ticket, payload: Any) -> Settlement:
        """Settle a call result against its ticket. Fenced results are recorded, never applied."""
        with self._guarded():
            settlement = self._submit(ticket, payload)
            size = _size(payload) if settlement.accepted else None
            if size is not None:
                reason = self._ledger.add_output(size)
                if reason is not None and self._life.state == "running":
                    self._life.finish("budget_exhausted", reason)
                    return Settlement(False, "budget_exhausted")
            return settlement

    def call_model(
        self,
        role: str,
        request: dict[str, Any],
        validate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Call the provider within budget, retrying timeouts, transient and malformed results.

        Raises ``RunStopped`` when the run stops (budget, timeout, cancel, failure) and
        ``ResultFenced`` when the result belongs to a call that is no longer current.
        """
        if self._provider is None:
            raise ValueError("no model provider configured")
        try:
            return self._call_model(self._provider, role, request, validate)
        finally:
            self._release_if_terminal()

    def call_tool(
        self, tool: str, arguments: dict[str, Any], skill_id: str | None = None
    ) -> ToolResult:
        """Execute one tool call (one execution step). Tool calls are never retried here."""
        if self._tools is None:
            raise ValueError("no tool executor configured")
        try:
            return self._call_tool(self._tools, tool, arguments, skill_id)
        finally:
            self._release_if_terminal()

    # --------------------------------------------------------------------------- internals
    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int or now < 0:
            raise ValueError("clock returned an invalid time")
        return now

    def _open_attempt(self) -> str:
        self._ledger.attempts += 1
        self._attempt_id = f"a{self._ledger.attempts}"
        return self._attempt_id

    def _abandon_open(self) -> None:
        self._abandoned.update(self._open)
        self._open.clear()

    def _stopped(self) -> RunStopped:
        return RunStopped(self._life.state, self._life.status, self._life.detail)

    def _terminate(self, status: str, detail: str) -> None:
        """Stop the run (``cancelling`` runs only accept ``failed``) and raise ``RunStopped``."""
        if self._life.state == "running" or (
            status == "failed" and self._life.state == "cancelling"
        ):
            self._life.finish(status, detail)
        raise self._stopped()

    def _require_running(self) -> int:
        if self._life.state != "running" or self._started_ms is None:
            raise self._stopped()
        now = self._now()
        if now - self._started_ms >= self.budgets.wall_ms:
            self._terminate("timeout", "wall_time")
        return now

    def _ticket(self, kind: str) -> Ticket:
        if kind not in ("model", "tool"):
            raise ValueError("ticket kind must be 'model' or 'tool'")
        now = self._require_running()
        if kind == "model":
            reason = self._ledger.before_model_call()
            per_call = self.budgets.model_call_timeout_ms
        else:
            reason = self._ledger.before_step()
            per_call = self.budgets.tool_call_timeout_ms
        if reason is not None:
            self._terminate("budget_exhausted", reason)
        if kind == "model":
            self._ledger.model_calls += 1
        else:
            self._ledger.steps += 1
        assert self._started_ms is not None and self._attempt_id is not None
        remaining = self._started_ms + self.budgets.wall_ms - now
        timeout = remaining if per_call is None else min(per_call, remaining)
        self._seq += 1
        ticket = Ticket(
            self.run_id, self._attempt_id, f"{kind[0]}{self._seq}", kind, now + timeout, timeout
        )
        self._open[ticket.call_id] = ticket
        return ticket

    def _fence_reason(self, ticket: Any, now: int) -> str | None:
        if not isinstance(ticket, Ticket):
            return "malformed_ticket"
        if ticket.run_id != self.run_id:
            return "run_mismatch"
        if ticket.attempt_id != self._attempt_id:
            return "attempt_mismatch"
        if ticket.call_id in self._settled:
            return "already_settled"
        if ticket.call_id in self._abandoned:
            return "call_abandoned"
        if self._open.get(ticket.call_id) != ticket:
            return "unknown_call"
        if self._life.state != "running":
            return "not_running"
        if now > ticket.deadline_ms:
            return "deadline_exceeded"
        return None

    def _submit(self, ticket: Ticket, payload: Any) -> Settlement:
        now = self._now()
        reason = self._fence_reason(ticket, now)
        if isinstance(ticket, Ticket) and ticket.run_id == self.run_id:
            if self._open.get(ticket.call_id) == ticket:
                del self._open[ticket.call_id]
                self._settled.add(ticket.call_id)
        if reason is None:
            return Settlement(True, None)
        record = {
            "reason": reason,
            "state": self._life.state,
            "run_id": getattr(ticket, "run_id", None),
            "attempt_id": getattr(ticket, "attempt_id", None),
            "call_id": getattr(ticket, "call_id", None),
            "kind": getattr(ticket, "kind", None),
            "payload_sha256": None if payload is None else _digest(payload),
            "at_ms": now,
        }
        self._fenced.append(record)
        self._life.diagnose("late_result_fenced", reason=reason, call_id=record["call_id"])
        return Settlement(False, reason)

    def _retry_or_stop(self, ticket: Ticket, cause: str) -> None:
        self._require_running()
        reason = self._ledger.before_retry()
        if reason is not None:
            self._terminate("budget_exhausted", reason)
        self._ledger.retries += 1
        self._life.diagnose("retry", cause=cause, call_id=ticket.call_id)

    def _call_model(
        self,
        provider: ModelProvider,
        role: str,
        request: dict[str, Any],
        validate: Callable[[dict[str, Any]], bool] | None,
    ) -> dict[str, Any]:
        while True:
            with self._lock:
                ticket = self._ticket("model")
            output: Any = None
            error: str | None = None
            fatal: str | None = None
            try:
                sent = copy.deepcopy(request)
                if isinstance(provider, DeadlineProvider):
                    output = provider.request_within(sent, ticket.timeout_ms)
                else:
                    output = provider.request(sent)
            except ProviderTimeout:
                error = "provider_timeout"
            except TransientProviderError:
                error = "provider_transient"
            except WorkerFailure:
                fatal = "worker_failure"
            except Exception:
                fatal = "provider_error"
            with self._lock:
                usage_reason = None
                if isinstance(output, dict):
                    # Usage is real consumption even when the result is fenced or invalid.
                    try:
                        usage = parse_usage(output)
                    except BudgetError:
                        usage, error = Usage(None, None), error or "invalid_output"
                    usage_reason = self._ledger.add_usage(usage)
                size = _size(output) if output is not None else 0
                bytes_reason = self._ledger.add_output(size or 0)
                settlement = self._submit(ticket, output)
                if not settlement.accepted:
                    if settlement.reason != "deadline_exceeded" and fatal is None:
                        if self._life.state == "running":
                            raise ResultFenced(settlement.reason or "fenced")
                        raise self._stopped()
                    error = error or "provider_timeout"
                if fatal is not None:
                    self._terminate("failed", fatal)
                if usage_reason is not None or bytes_reason is not None:
                    self._terminate("budget_exhausted", usage_reason or bytes_reason or "")
                if error is None:
                    if (
                        not isinstance(output, dict)
                        or size is None
                        or size > self.budgets.max_output_bytes
                    ):
                        error = "invalid_output"
                    elif validate is not None:
                        try:
                            ok = validate(copy.deepcopy(output))
                        except Exception:
                            ok = False
                        if ok is not True:
                            error = "invalid_output"
                if error is None:
                    assert isinstance(output, dict)
                    return copy.deepcopy(output)
                self._retry_or_stop(ticket, error)

    def _call_tool(
        self, tools: ToolExecutor, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        with self._lock:
            ticket = self._ticket("tool")
        result: Any = None
        timed_out = False
        fatal: str | None = None
        try:
            result = tools.execute(ticket.call_id, tool, copy.deepcopy(arguments), skill_id)
        except ProviderTimeout:
            timed_out = True
        except Exception:
            fatal = "worker_failure"
        with self._lock:
            valid = (
                isinstance(result, tuple)
                and len(result) == 3
                and result[0] in TOOL_RESULT_STATUSES
                and isinstance(result[1], dict)
                and isinstance(result[2], dict)
            )
            payload = {"status": result[0], "result": result[1]} if valid else None
            settlement = self._submit(ticket, payload)
            size = _size(payload) if payload is not None else None
            bytes_reason = self._ledger.add_output(size or 0)
            if not settlement.accepted:
                if settlement.reason == "deadline_exceeded":
                    self._terminate("timeout", "tool_deadline")
                if self._life.state == "running":
                    raise ResultFenced(settlement.reason or "fenced")
                raise self._stopped()
            if timed_out:
                self._terminate("timeout", "tool_deadline")
            if fatal is not None:
                self._terminate("failed", fatal)
            if not valid or size is None:
                self._terminate("failed", "malformed_tool_result")
            if size is not None and size > self.budgets.max_output_bytes:
                self._terminate("failed", "oversized_tool_result")
            if bytes_reason is not None:
                self._terminate("budget_exhausted", bytes_reason)
            return ToolResult(result[0], copy.deepcopy(result[1]), copy.deepcopy(result[2]))

    def _release_if_terminal(self) -> None:
        with self._lock:
            if not self._life.terminal or self._release_started:
                return
            self._release_started = True
            self._abandon_open()
        self._release_resources()

    def _release_resources(self) -> None:
        with self._lock:
            resources = list(reversed(self._resources))
            self._resources.clear()
        for name, release in resources:
            try:
                release()
            except Exception:
                self._life.diagnose("release_failed", resource=name)
                continue
            with self._lock:
                self._released.append(name)
