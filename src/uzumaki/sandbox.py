"""Restricted worker for untrusted candidate code (feature 06).

``RestrictedWorker.run(source, payload)`` executes candidate source in a fresh child process that
contains itself with kernel mechanisms before it reads the candidate (see
``uzumaki._sandbox_boot``):

- Landlock (ABI 6 or later): read-only access to the Python standard library and system
  libraries, read-only access to the candidate's own directory, read-write access to one fresh
  scratch directory, and nothing else. TCP bind/connect are denied, and signals and abstract
  UNIX sockets cannot reach processes outside the worker.
- seccomp: no sockets at all, no new processes or ``exec`` (threads only), no namespaces,
  ``ptrace``, ``setsid``/``setpgid``, rlimit changes, mounts, BPF, io_uring or keyrings.
- rlimits: CPU seconds, address space, file size, open files and no core dumps.
- A scrubbed environment (no credentials), no inherited descriptors except the broker pipes,
  ``no_new_privs`` and a new session.

The host keeps watchdog and kill authority. A timer thread outside the worker kills the whole
process group at the wall deadline, output and broker messages are read up to fixed byte limits,
and after every run the group is killed, the worker is reaped and ``/proc`` is checked for
survivors. Only tools declared by the ``ToolBroker`` are reachable, and their handlers run in the
host.

Admission is strict: only the verified platform (Linux x86_64 with Landlock ABI 6+ and seccomp
filters) is accepted, and anything else raises ``ContainmentUnavailable`` before any process is
started. Python-level restrictions and schema validation are not treated as isolation.

The candidate's return value is untrusted data, never evidence of task success.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uzumaki.broker import BROKER_PROTOCOL, ToolBroker, ToolCallRecord

SANDBOX_VERSION = "uzumaki.sandbox/1.0"

#: Platforms on which the adversarial containment suite has been run and passed.
VERIFIED_PLATFORMS = (("Linux", "x86_64"),)
#: Landlock ABI 6 adds signal and abstract-socket scoping; must equal the bootstrap's minimum.
MIN_LANDLOCK_ABI = 6

#: Worker statuses. ``returned`` only means the candidate's ``run`` returned a JSON value.
STATUSES = (
    "returned",
    "candidate_error",
    "timeout",
    "cpu_limit",
    "output_limit",
    "protocol_violation",
    "tool_budget_exhausted",
    "crashed",
    "containment_failed",
)

_SYSTEM_LIBRARY_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")
_SYSTEM_FILES = ("/etc/ld.so.cache",)
# Started by path, never imported here: it needs Linux-only modules and restricts its process.
_BOOT_SCRIPT = Path(__file__).resolve().with_name("_sandbox_boot.py")
_ERROR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_REAP_SECONDS = 5.0


class ContainmentUnavailable(Exception):
    """Execution refused: the platform or configuration cannot provide the verified boundary."""


@dataclass(frozen=True)
class PlatformReport:
    system: str
    machine: str
    kernel: str
    landlock_abi: int
    seccomp_filter: bool
    python: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "machine": self.machine,
            "kernel": self.kernel,
            "landlock_abi": self.landlock_abi,
            "seccomp_filter": self.seccomp_filter,
            "python": self.python,
        }


def _landlock_abi() -> int:
    """Highest Landlock ABI the running kernel supports, or 0 (a query; restricts nothing)."""
    if sys.platform != "linux":
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        abi = int(libc.syscall(444, None, 0, 1))  # landlock_create_ruleset(..., VERSION)
    except (OSError, AttributeError):
        return 0
    return abi if abi > 0 else 0


def _seccomp_filter_supported() -> bool:
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return False
    return any(line.startswith("Seccomp:") for line in text.splitlines())


def probe_platform() -> PlatformReport:
    """Describe this host. Probing has no side effects and starts no process."""
    system = platform.system()
    linux = system == "Linux"
    return PlatformReport(
        system=system,
        machine=platform.machine(),
        kernel=platform.release(),
        landlock_abi=_landlock_abi(),
        seccomp_filter=linux and _seccomp_filter_supported(),
        python=platform.python_version(),
    )


def admit(report: PlatformReport) -> PlatformReport:
    """Return ``report`` if it is a verified platform, else raise ``ContainmentUnavailable``."""
    if report.system != "Linux":
        raise ContainmentUnavailable("unsupported_os")
    if (report.system, report.machine) not in VERIFIED_PLATFORMS:
        raise ContainmentUnavailable("unverified_architecture")
    if report.landlock_abi < MIN_LANDLOCK_ABI:
        raise ContainmentUnavailable("landlock_abi_too_old")
    if not report.seccomp_filter:
        raise ContainmentUnavailable("seccomp_unavailable")
    return report


@dataclass(frozen=True)
class WorkerLimits:
    """Per-run limits. All values are strict positive integers."""

    wall_ms: int = 10_000
    cpu_seconds: int = 5
    memory_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 64 * 1024
    max_message_bytes: int = 64 * 1024
    max_tool_calls: int = 64
    max_file_bytes: int = 1024 * 1024
    max_open_files: int = 64

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.memory_bytes < 64 * 1024 * 1024:
            raise ValueError("memory_bytes must be at least 64 MiB for the interpreter")
        if self.max_open_files < 16:
            raise ValueError("max_open_files must be at least 16")


@dataclass(frozen=True)
class WorkerReport:
    """Outcome of one worker run. ``result`` is untrusted candidate output."""

    status: str
    result: Any
    error: str | None
    exit_code: int | None
    signal: int | None
    tool_calls: tuple[ToolCallRecord, ...]
    output: bytes
    output_truncated: bool
    tree_clean: bool
    wall_ms: int
    cpu_ms: int
    landlock_abi: int | None
    platform: PlatformReport
    limits: WorkerLimits
    sandbox_version: str = SANDBOX_VERSION
    broker_manifest: dict[str, Any] = field(default_factory=dict)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _read_paths() -> list[str]:
    base = {
        "base": sys.base_prefix,
        "platbase": sys.base_exec_prefix,
        "installed_base": sys.base_prefix,
        "installed_platbase": sys.base_exec_prefix,
    }
    paths = sysconfig.get_paths(vars=base)
    found: list[str] = []
    for path in (paths["stdlib"], paths["platstdlib"], *_SYSTEM_LIBRARY_DIRS, *_SYSTEM_FILES):
        resolved = os.path.realpath(path)
        if os.path.exists(resolved) and resolved not in found:
            found.append(resolved)
    return found


def _group_members(pgid: int) -> list[int]:
    """PIDs still running (not zombies) in process group ``pgid``, from ``/proc``."""
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            members.append(int(entry))
    return members


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Session:
    """State of one run in the host. Everything here runs outside the worker."""

    def __init__(self, broker: ToolBroker, limits: WorkerLimits) -> None:
        self.broker = broker
        self.limits = limits
        self.lock = threading.Lock()
        self.reaped = False
        self.stopping: str | None = None
        self.ready = False
        self.landlock_abi: int | None = None
        self.final: dict[str, Any] | None = None
        self.containment_reason: str | None = None
        self.calls: list[ToolCallRecord] = []
        self.output = bytearray()
        self.truncated = False
        self.outbox = bytearray()

    def stop(self, pgid: int, reason: str) -> None:
        """Kill the worker group once, recording the first reason. Safe from any thread."""
        with self.lock:
            if self.stopping is None:
                # A watchdog firing after the final message must not relabel a finished run.
                late = reason == "timeout" and self.final is not None
                self.stopping = "cleanup" if late else reason
            if not self.reaped:
                _kill_group(pgid)

    def on_output(self, data: bytes) -> bool:
        room = self.limits.max_output_bytes - len(self.output)
        self.output += data[: max(room, 0)]
        if len(data) > room:
            self.truncated = True
            return False
        return True

    def on_message(self, line: bytes) -> str | None:
        """Handle one broker line. Returns a kill reason, or None to continue."""
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return "protocol_violation"
        if not isinstance(message, dict):
            return "protocol_violation"
        kind = message.get("type")
        if not self.ready:
            if kind == "ready" and message.get("protocol") == BROKER_PROTOCOL:
                abi = message.get("landlock_abi")
                if type(abi) is int and abi >= MIN_LANDLOCK_ABI:
                    self.ready = True
                    self.landlock_abi = abi
                    return None
            if kind == "containment_failed":
                reason = message.get("reason")
                self.containment_reason = reason if isinstance(reason, str) else None
            return "containment_failed"
        if self.final is not None:
            return "protocol_violation"
        if kind == "tool":
            if len(self.calls) >= self.limits.max_tool_calls:
                return "tool_budget_exhausted"
            response, record = self.broker.handle(
                len(self.calls) + 1, message, self.limits.max_message_bytes // 2
            )
            self.calls.append(record)
            data = json.dumps(response, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            self.outbox += data
            return None
        if (kind == "result" and set(message) == {"type", "value"}) or (
            kind == "error" and set(message) == {"type", "error"}
        ):
            with self.lock:
                if self.stopping is None:
                    self.final = message
            return None
        return "protocol_violation"


class RestrictedWorker:
    """Run untrusted candidate source in the verified OS boundary with a versioned broker.

    ``protected_paths`` (evaluator, prompts, skills, held-out assets, credentials) must not
    overlap anything the worker may read or write, or the run is refused. Landlock is an
    allowlist, so everything outside the allowed paths is unreachable; the worker proves the
    domain is active by failing to open a host canary file before the candidate is loaded.
    Protected path names are never passed to the worker.
    """

    def __init__(
        self,
        broker: ToolBroker,
        limits: WorkerLimits | None = None,
        *,
        protected_paths: Iterable[str | os.PathLike[str]] = (),
        probe: Callable[[], PlatformReport] = probe_platform,
        work_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if not isinstance(broker, ToolBroker):
            raise ValueError("broker must be a ToolBroker")
        self.broker = broker
        self.limits = limits if limits is not None else WorkerLimits()
        self.protected = tuple(Path(os.path.realpath(p)) for p in protected_paths)
        self._probe = probe
        self._work_root = None if work_root is None else os.fspath(work_root)

    def run(self, source: str, payload: Any = None) -> WorkerReport:
        """Execute ``source`` (which defines ``run(tools, payload)``) and report the outcome.

        Raises ``ContainmentUnavailable`` before starting any process when the platform is not
        verified or the configuration would expose a protected path.
        """
        if not isinstance(source, str):
            raise ValueError("source must be a string")
        report = admit(self._probe())
        payload_text = json.dumps(payload, allow_nan=False)
        read_paths = _read_paths()
        work = Path(os.path.realpath(tempfile.mkdtemp(prefix="uzumaki-", dir=self._work_root)))
        try:
            for protected in self.protected:
                exposed = [work, *(Path(p) for p in read_paths)]
                if any(_overlaps(protected, path) for path in exposed):
                    raise ContainmentUnavailable("protected_path_exposed")
            candidate_dir = work / "candidate"
            scratch = work / "scratch"
            candidate_dir.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            (candidate_dir / "candidate.py").write_text(source, encoding="utf-8")
            (candidate_dir / "payload.json").write_text(payload_text, encoding="utf-8")
            # A file outside every allowed path: the worker must fail to open it before the
            # candidate is loaded, or the run fails closed as containment_failed.
            canary = work / "canary"
            canary.write_text("canary", encoding="ascii")
            return self._execute(
                report, candidate_dir, scratch, canary, [*read_paths, str(candidate_dir)]
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _execute(
        self,
        report: PlatformReport,
        candidate_dir: Path,
        scratch: Path,
        canary: Path,
        read_paths: list[str],
    ) -> WorkerReport:
        limits = self.limits
        manifest = self.broker.manifest()
        session = _Session(self.broker, limits)
        req_r, req_w = os.pipe()
        resp_r, resp_w = os.pipe()
        out_r, out_w = os.pipe()
        config = {
            "protocol": BROKER_PROTOCOL,
            "request_fd": req_w,
            "response_fd": resp_r,
            "candidate": str(candidate_dir / "candidate.py"),
            "payload": str(candidate_dir / "payload.json"),
            "scratch": str(scratch),
            "read_paths": read_paths,
            "probe_paths": [str(canary)],
            "tools": manifest["tools"],
            "max_message_bytes": limits.max_message_bytes,
            "limits": {
                "cpu_seconds": limits.cpu_seconds,
                "memory_bytes": limits.memory_bytes,
                "max_file_bytes": limits.max_file_bytes,
                "max_open_files": limits.max_open_files,
            },
        }
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", str(_BOOT_SCRIPT), json.dumps(config)],
                stdin=subprocess.DEVNULL,
                stdout=out_w,
                stderr=out_w,
                pass_fds=(req_w, resp_r),
                close_fds=True,
                cwd=str(scratch),
                env={"LANG": "C.UTF-8"},
                start_new_session=True,
            )
        finally:
            for fd in (req_w, resp_r, out_w):
                os.close(fd)
        pgid = proc.pid
        watchdog = threading.Timer(limits.wall_ms / 1000.0, session.stop, (pgid, "timeout"))
        watchdog.daemon = True
        watchdog.start()
        try:
            self._pump(session, pgid, req_r, resp_w, out_r, start)
        finally:
            watchdog.cancel()
            # Kill the group before reaping: the unreaped worker PID keeps the group ID from
            # being reused, so this cannot hit an unrelated process.
            session.stop(pgid, "cleanup")
            status_code, cpu_ms = self._reap(proc)
            with session.lock:
                session.reaped = True
            for fd in (req_r, resp_w, out_r):
                os.close(fd)
            tree_clean = self._sweep(pgid)
        wall_ms = int((time.monotonic() - start) * 1000)
        return self._report(session, report, proc, status_code, cpu_ms, tree_clean, wall_ms)

    def _pump(
        self, session: _Session, pgid: int, req_r: int, resp_w: int, out_r: int, start: float
    ) -> None:
        limits = session.limits
        os.set_blocking(resp_w, False)
        selector = selectors.DefaultSelector()
        selector.register(req_r, selectors.EVENT_READ, "req")
        selector.register(out_r, selectors.EVENT_READ, "out")
        open_streams = 2
        pending = bytearray()
        deadline = start + limits.wall_ms / 1000.0
        writing = False

        def drain() -> None:
            # Strict request/response: no request is handled while a response is still being
            # written, so a worker that never reads its answers cannot grow host memory.
            while b"\n" in pending and not session.outbox and session.stopping is None:
                end = pending.index(b"\n")
                line = bytes(pending[:end])
                del pending[: end + 1]
                reason = session.on_message(line)
                if reason is not None:
                    session.stop(pgid, reason)
            if len(pending) > limits.max_message_bytes:
                session.stop(pgid, "protocol_violation")

        try:
            while open_streams and session.stopping is None and session.final is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    session.stop(pgid, "timeout")
                    break
                for key, _ in selector.select(min(remaining, 0.5)):
                    if key.data == "resp":
                        try:
                            del session.outbox[: os.write(resp_w, session.outbox)]
                        except BlockingIOError:
                            pass
                        except BrokenPipeError:
                            session.outbox.clear()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fd)
                        open_streams -= 1
                    elif key.data == "out":
                        if not session.on_output(chunk):
                            session.stop(pgid, "output_limit")
                    else:
                        pending += chunk
                if not session.outbox and writing:
                    selector.unregister(resp_w)
                    writing = False
                drain()
                if session.outbox and not writing:
                    selector.register(resp_w, selectors.EVENT_WRITE, "resp")
                    writing = True
        finally:
            selector.close()

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes]) -> tuple[int | None, int]:
        end = time.monotonic() + _REAP_SECONDS
        while True:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                proc.returncode = os.waitstatus_to_exitcode(status)
                return proc.returncode, int((usage.ru_utime + usage.ru_stime) * 1000)
            if time.monotonic() > end:  # pragma: no cover - SIGKILL did not take effect
                raise ContainmentUnavailable("worker_not_reaped")
            time.sleep(0.01)

    @staticmethod
    def _sweep(pgid: int) -> bool:
        """Verify that no process of the worker group survives; kill stragglers."""
        end = time.monotonic() + _REAP_SECONDS
        while _group_members(pgid):
            if time.monotonic() > end:
                return False
            _kill_group(pgid)
            time.sleep(0.02)
        return True

    def _report(
        self,
        session: _Session,
        platform_report: PlatformReport,
        proc: subprocess.Popen[bytes],
        exit_code: int | None,
        cpu_ms: int,
        tree_clean: bool,
        wall_ms: int,
    ) -> WorkerReport:
        signum = -exit_code if exit_code is not None and exit_code < 0 else None
        result: Any = None
        error: str | None = None
        reason = session.stopping if session.stopping != "cleanup" else None
        if not session.ready:
            status = "containment_failed"
            error = session.containment_reason
        elif reason is not None:
            status = reason
        elif session.final is not None:
            if session.final["type"] == "result":
                status, result = "returned", session.final["value"]
            else:
                status = "candidate_error"
                raw = session.final["error"]
                error = raw if isinstance(raw, str) and _ERROR_NAME.fullmatch(raw) else "invalid"
        elif signum == signal.SIGXCPU or (
            signum == signal.SIGKILL and cpu_ms >= self.limits.cpu_seconds * 1000
        ):
            status = "cpu_limit"
        else:
            status = "crashed"
        if status == "containment_failed" and error is not None:
            error = error if _ERROR_NAME.fullmatch(error) else "invalid"
        return WorkerReport(
            status=status,
            result=result,
            error=error,
            exit_code=exit_code if signum is None else None,
            signal=signum,
            tool_calls=tuple(session.calls),
            output=bytes(session.output),
            output_truncated=session.truncated,
            tree_clean=tree_clean,
            wall_ms=wall_ms,
            cpu_ms=cpu_ms,
            landlock_abi=session.landlock_abi,
            platform=platform_report,
            limits=self.limits,
            broker_manifest=self.broker.manifest(),
        )
