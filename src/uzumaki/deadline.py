"""Real-process deadlines: run one call in a child process and kill it when time runs out.

``run_in_process`` starts a fresh (spawned) child in its own process group, waits at most
``timeout_ms`` for one bounded, JSON-encoded reply, and always kills the group and reaps the child
before returning, so a hung, crashed or flooding call cannot hold the parent or leave descendants
behind. A watchdog timer outside the child enforces the deadline, so the parent stays responsive
however the child behaves.

This bounds time and output and releases processes. It is not a sandbox: the child has the
parent's privileges, filesystem and network. Restricted workers for generated code are feature
06. POSIX only.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from uzumaki.faults import ProviderTimeout, WorkerFailure

_REAP_SECONDS = 5.0


def _child(target: Callable[[Any], Any], payload: Any, conn: Connection) -> None:
    try:
        os.setpgid(0, 0)
    except OSError:  # pragma: no cover - already a group leader
        pass
    try:
        reply = {"ok": target(payload)}
        data = json.dumps(reply, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except BaseException:
        data = b'{"error":"worker_exception"}'
    conn.send_bytes(data)
    conn.close()


def _kill_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_in_process(
    target: Callable[[Any], Any], payload: Any, *, timeout_ms: int, max_output_bytes: int
) -> Any:
    """Return ``target(payload)`` computed in a child process within ``timeout_ms``.

    ``target`` must be an importable module-level function and its result JSON-serializable.
    Raises ``ProviderTimeout`` when the deadline passes and ``WorkerFailure`` when the child
    crashes, raises, exits without replying, or replies with oversized or malformed output.
    """
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(target, payload, writer), daemon=True)
    expired = threading.Event()
    guard = threading.Lock()
    finished = False

    def expire() -> None:
        with guard:
            if finished:
                return
            expired.set()
            _kill_group(proc.pid)
            proc.kill()

    watchdog = threading.Timer(timeout_ms / 1000.0, expire)
    watchdog.daemon = True
    try:
        proc.start()
        writer.close()
        watchdog.start()
        # The envelope adds at most 8 bytes ({"ok":...}) around the result.
        try:
            raw = reader.recv_bytes(max_output_bytes + 8)
        except EOFError:
            raw = None
        except OSError:
            raise WorkerFailure("oversized_output") from None
    finally:
        watchdog.cancel()
        with guard:
            finished = True
        reader.close()
        if proc.pid is not None:
            # Kill the whole group (the child is not reaped yet, so its PID cannot be reused),
            # then reap the child. Grandchildren in the group die with it.
            _kill_group(proc.pid)
            proc.kill()
            proc.join(_REAP_SECONDS)
            if proc.exitcode is None:  # pragma: no cover - SIGKILL did not take effect
                raise WorkerFailure("worker_not_reaped")
            proc.close()
    if raw is None:
        if expired.is_set():
            raise ProviderTimeout("deadline_exceeded")
        raise WorkerFailure("worker_exited")
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WorkerFailure("malformed_output") from None
    if not isinstance(reply, dict) or set(reply) not in ({"ok"}, {"error"}):
        raise WorkerFailure("malformed_output")
    if "error" in reply:
        raise WorkerFailure("worker_exception")
    return reply["ok"]


class ProcessProvider:
    """Model provider whose every request runs in a deadline-bounded child process."""

    def __init__(
        self,
        target: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        timeout_ms: int,
        max_output_bytes: int,
    ) -> None:
        self._target = target
        self._timeout_ms = timeout_ms
        self._max_output_bytes = max_output_bytes
        self.calls = 0

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, self._timeout_ms)

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        self.calls += 1
        result = run_in_process(
            self._target,
            request,
            timeout_ms=max(1, min(timeout_ms, self._timeout_ms)),
            max_output_bytes=self._max_output_bytes,
        )
        if not isinstance(result, dict):
            raise WorkerFailure("malformed_output")
        return result
