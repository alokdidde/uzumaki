"""Adversarial containment tests on the real OS boundary (real processes, real clock).

These run only on a verified platform (Linux x86_64, Landlock ABI 6+, seccomp filters). Elsewhere
they are skipped with the admission reason, and the run itself is shown to fail admission. Set
``UZUMAKI_REQUIRE_CONTAINMENT=1`` (``make containment``) to turn that skip into a failure.
Timing bounds are generous ceilings for slow hosts, not latency claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from uzumaki.broker import pick_place_v1_broker
from uzumaki.sandbox import (
    ContainmentUnavailable,
    RestrictedWorker,
    WorkerLimits,
    WorkerReport,
    admit,
    probe_platform,
)

CEILING_S = 20.0
SECRET = "uzumaki-test-secret-7f3a"


def _admission() -> str | None:
    try:
        admit(probe_platform())
    except ContainmentUnavailable as error:
        return str(error)
    return None


UNSUPPORTED = _admission()
if UNSUPPORTED is not None and os.environ.get("UZUMAKI_REQUIRE_CONTAINMENT") == "1":
    raise RuntimeError(f"containment required but platform not admitted: {UNSUPPORTED}")

pytestmark = pytest.mark.skipif(
    UNSUPPORTED is not None, reason=f"unverified platform: {UNSUPPORTED}"
)


def group_members(pgid: int) -> list[int]:
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path(f"/proc/{entry}/stat").read_text(encoding="ascii", errors="replace")
            except OSError:
                continue
            fields = stat.rsplit(")", 1)[-1].split()
            if fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
                found.append(int(entry))
    return found


class Recorder:
    """Host tool handlers that record what reached them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, name: str) -> Any:
        def handle(args: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, args))
            return {"tool": name, "ok": True}

        return handle


@pytest.fixture
def host() -> Recorder:
    return Recorder()


@pytest.fixture
def worker(host: Recorder, tmp_path: Path) -> Iterator[Any]:
    created: list[int] = []
    real_popen = subprocess.Popen

    class TrackingPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self.pid)

    def make(limits: WorkerLimits | None = None, protected: tuple[Path, ...] = ()) -> Any:
        broker = pick_place_v1_broker({n: host.handler(n) for n in ("observe", "pick", "place")})
        w = RestrictedWorker(broker, limits, protected_paths=protected)

        def run(source: str, payload: Any = None) -> WorkerReport:
            subprocess.Popen = TrackingPopen  # type: ignore[misc]
            try:
                return w.run(source, payload)
            finally:
                subprocess.Popen = real_popen  # type: ignore[misc]

        return run

    yield make
    # Every worker group is gone after every run, whatever the candidate did.
    for pid in created:
        assert group_members(pid) == []


def candidate(body: str) -> str:
    """A candidate whose ``run`` tries each named attempt and reports the exception type."""
    return (
        "import os, sys, json, socket, subprocess, ctypes, resource, signal, threading\n"
        "CONFIG = json.loads(sys.argv[1])\n"
        "def attempt(fn):\n"
        "    try:\n"
        "        value = fn()\n"
        "    except BaseException as error:\n"
        "        return 'denied:' + type(error).__name__\n"
        "    return 'allowed:' + repr(value)[:200]\n"
        "def run(tools, payload):\n"
        "    out = {}\n"
        + "".join(f"    {line}\n" for line in body.strip().splitlines())
        + "    return out\n"
    )


def digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- nominal behaviour --------------------------------------------------------------------------


def test_declared_tools_reach_host_handlers_and_the_result_is_returned(
    worker: Any, host: Recorder
) -> None:
    source = candidate(
        """
out['observe'] = tools.call('observe', {'camera': 1})
out['pick'] = tools.call('pick', {'object': 'cube'})
out['payload'] = payload
out['scratch'] = attempt(lambda: open('note.txt', 'w').write('ok'))
out['readback'] = attempt(lambda: open('note.txt').read())
t = threading.Thread(target=lambda: out.__setitem__('thread', 'ran')); t.start(); t.join()
print('hello from candidate')
"""
    )
    report = worker()(source, {"case": 3})
    assert report.status == "returned"
    assert report.result["observe"]["result"] == {"tool": "observe", "ok": True}
    assert report.result["pick"]["status"] == "ok"
    assert report.result["payload"] == {"case": 3}
    assert report.result["scratch"] == "allowed:2"
    assert report.result["readback"] == "allowed:'ok'"
    assert report.result["thread"] == "ran"
    assert report.output == b"hello from candidate\n"
    assert host.calls == [("observe", {"camera": 1}), ("pick", {"object": "cube"})]
    assert report.tree_clean and report.landlock_abi is not None and report.landlock_abi >= 6
    assert [c.status for c in report.tool_calls] == ["ok", "ok"]


def test_undeclared_tools_and_wrong_versions_never_reach_the_host(
    worker: Any, host: Recorder
) -> None:
    source = candidate(
        """
for name in ('write_evaluator', 'read_heldout', 'update_prompt', 'register_skill', 'abort'):
    out[name] = tools.call(name, {})['reason']
out['old'] = tools.call('pick', {}, version='0.9')['reason']
"""
    )
    report = worker()(source)
    assert report.status == "returned"
    assert report.result == {
        "write_evaluator": "unknown_tool",
        "read_heldout": "unknown_tool",
        "update_prompt": "unknown_tool",
        "register_skill": "unknown_tool",
        "abort": "unknown_tool",  # declared by the profile but not provided by this host
        "old": "version_mismatch",
    }
    assert host.calls == []
    assert [c.reason for c in report.tool_calls][-1] == "version_mismatch"


# --- file escape and protected assets -----------------------------------------------------------


def test_file_escape_cannot_read_or_write_protected_assets(worker: Any, tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    for name in ("evaluator", "prompts", "skills", "heldout"):
        (assets / name).mkdir(parents=True)
        (assets / name / "data.json").write_text('{"answer": 42}', encoding="utf-8")
    before = digest_tree(assets)
    evaluator = assets / "evaluator" / "data.json"
    repo_file = Path(__file__).resolve()
    source = candidate(
        f"""
out['read_evaluator'] = attempt(lambda: open({str(evaluator)!r}).read())
out['read_heldout'] = attempt(lambda: open({str(assets / "heldout" / "data.json")!r}).read())
out['list_assets'] = attempt(lambda: os.listdir({str(assets)!r}))
out['write_evaluator'] = attempt(lambda: open({str(evaluator)!r}, 'w').write('{{}}'))
out['new_prompt'] = attempt(lambda: open({str(assets / "prompts" / "new.txt")!r}, 'w'))
out['new_skill_dir'] = attempt(lambda: os.mkdir({str(assets / "skills" / "evil")!r}))
out['delete_heldout'] = attempt(lambda: os.unlink({str(assets / "heldout" / "data.json")!r}))
out['rename_in'] = attempt(lambda: os.rename({str(evaluator)!r}, 'stolen.json'))
out['hardlink_in'] = attempt(lambda: os.link({str(evaluator)!r}, 'linked.json'))
out['symlink'] = attempt(lambda: os.symlink({str(evaluator)!r}, 'sym.json'))
out['traversal'] = attempt(lambda: open('../../' + 'escape.txt', 'w'))
out['repo_source'] = attempt(lambda: open({str(repo_file)!r}).read())
out['etc_passwd'] = attempt(lambda: open('/etc/passwd').read())
out['proc_self'] = attempt(lambda: open('/proc/self/status').read())
out['dev_mem'] = attempt(lambda: open('/dev/mem', 'rb'))
out['host_tmp_write'] = attempt(lambda: open({str(tmp_path / "escape.txt")!r}, 'w'))
out['candidate_write'] = attempt(lambda: open(CONFIG['candidate'], 'a'))
out['boot_write'] = attempt(lambda: open(sys.argv[0], 'a'))
"""
    )
    report = worker(protected=(assets,))(source)
    assert report.status == "returned"
    leaked = {k: v for k, v in report.result.items() if not v.startswith("denied:")}
    assert leaked == {}
    assert "42" not in repr(report.result)
    assert digest_tree(assets) == before
    assert not (tmp_path / "escape.txt").exists()


# --- credentials --------------------------------------------------------------------------------


def test_credentials_are_not_visible(
    worker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text(f"secret={SECRET}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    source = candidate(
        f"""
out['env'] = dict(os.environ)
out['home_file'] = attempt(lambda: open({str(home / ".aws" / "credentials")!r}).read())
out['parent_environ'] = attempt(lambda: open('/proc/%d/environ' % os.getppid(), 'rb').read())
out['self_environ'] = attempt(lambda: open('/proc/self/environ', 'rb').read())
out['ssh'] = attempt(lambda: os.listdir(os.path.expanduser('~/.ssh')))
"""
    )
    report = worker()(source)
    assert report.status == "returned"
    assert report.result["env"] == {"LANG": "C.UTF-8"}
    assert all(report.result[k].startswith("denied:") for k in report.result if k != "env")
    assert SECRET not in repr(report)


# --- network ------------------------------------------------------------------------------------


def _host_listener(family: int, address: Any) -> socket.socket | None:
    """A non-blocking host listener for the candidate to try, or None if the host has no network.

    Some validation hosts run the whole test process under an inherited seccomp network guard, so
    the host itself cannot create sockets. The candidate's attempts are still made and must still be
    denied; only the "nothing reached the host listener" check is unavailable there.
    """
    try:
        server = socket.socket(family, socket.SOCK_STREAM)
    except PermissionError:
        return None
    try:
        server.bind(address)
        server.listen(8)
        server.setblocking(False)
    except OSError:
        server.close()
        return None
    return server


NETWORK_ATTEMPTS = ("tcp", "udp", "ipv6", "unix", "netlink", "pair", "raw_syscall", "io_uring")


def test_network_is_denied_even_through_raw_syscalls(worker: Any, tmp_path: Path) -> None:
    listener = _host_listener(socket.AF_INET, ("127.0.0.1", 0))
    # Without a host listener, port 9 (discard) is a target nothing should be able to connect to.
    port = listener.getsockname()[1] if listener is not None else 9
    unix_path = tmp_path / "host.sock"
    unix_listener = _host_listener(socket.AF_UNIX, str(unix_path))
    servers = [s for s in (listener, unix_listener) if s is not None]
    try:
        source = candidate(
            f"""
out['tcp'] = attempt(lambda: socket.create_connection(('127.0.0.1', {port}), timeout=2))
out['udp'] = attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
out['ipv6'] = attempt(lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
out['unix'] = attempt(lambda: socket.socket(socket.AF_UNIX).connect({str(unix_path)!r}))
out['netlink'] = attempt(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW))
out['pair'] = attempt(lambda: socket.socketpair())
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.syscall(41, 2, 1, 0)  # raw socket(AF_INET, SOCK_STREAM) bypassing the socket module
out['raw_syscall'] = 'denied:errno%d' % ctypes.get_errno() if fd < 0 else 'allowed:%d' % fd
fd = libc.syscall(425, 8, 0)  # io_uring_setup, which could open sockets without socket()
out['io_uring'] = 'denied:errno%d' % ctypes.get_errno() if fd < 0 else 'allowed:%d' % fd
"""
        )
        report = worker()(source)
        assert report.status == "returned"
        assert sorted(report.result) == sorted(NETWORK_ATTEMPTS)
        assert all(v.startswith("denied:") for v in report.result.values()), report.result
        assert report.result["raw_syscall"] == "denied:errno1"  # EPERM from the kernel
        for server in servers:
            with pytest.raises(BlockingIOError):
                server.accept()
    finally:
        for server in servers:
            server.close()


# --- processes and kill authority ---------------------------------------------------------------


def test_subprocess_and_privilege_escapes_are_denied(worker: Any) -> None:
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        source = candidate(
            f"""
out['subprocess'] = attempt(lambda: subprocess.run(['/bin/true']))
out['system'] = attempt(lambda: os.system('true') or 'zero-is-success')
out['fork'] = attempt(os.fork)
out['spawn'] = attempt(lambda: os.posix_spawn('/bin/true', ['true'], {{}}))
out['exec'] = attempt(lambda: os.execv(sys.executable, [sys.executable, '-c', 'pass']))
out['setsid'] = attempt(os.setsid)
out['setpgid'] = attempt(lambda: os.setpgid(0, 0))
out['kill_host_process'] = attempt(lambda: os.kill({victim.pid}, signal.SIGKILL))
out['signal_parent'] = attempt(lambda: os.kill(os.getppid(), 0))  # probe only: never kill pytest
out['kill_init'] = attempt(lambda: os.kill(1, 0))
out['raise_cpu_limit'] = attempt(lambda: resource.setrlimit(resource.RLIMIT_CPU, (1, 1)))
libc = ctypes.CDLL(None, use_errno=True)
raw = {{'unshare_user': (272, 0x10000000), 'ptrace_parent': (101, 16, os.getppid(), 0, 0)}}
for name, (nr, *args) in raw.items():
    rc = libc.syscall(nr, *args)
    out[name] = 'denied:errno%d' % ctypes.get_errno() if rc < 0 else 'allowed:%d' % rc
"""
        )
        report = worker()(source)
        assert report.status == "returned"
        allowed = {k: v for k, v in report.result.items() if not v.startswith("denied:")}
        # os.system reports a failed fork as a non-zero status rather than raising.
        assert allowed.pop("system", "allowed:-1") != "allowed:'zero-is-success'"
        assert allowed == {}
        assert victim.poll() is None  # the host process the candidate tried to kill survives
    finally:
        victim.kill()
        victim.wait()


@pytest.mark.parametrize(
    "loop",
    ["while True:\n        pass", "while True:\n        time.sleep(1)"],
    ids=["busy", "sleeping"],
)
def test_hung_worker_is_killed_by_the_host_watchdog_and_the_host_stays_responsive(
    worker: Any, loop: str
) -> None:
    ticks = 0
    stop = threading.Event()

    def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            time.sleep(0.05)

    thread = threading.Thread(target=ticker)
    thread.start()
    source = (
        "import signal, time\n"
        "def run(tools, payload):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    signal.signal(signal.SIGXCPU, signal.SIG_IGN)\n"
        f"    {loop}\n"
    )
    start = time.monotonic()
    try:
        report = worker(WorkerLimits(wall_ms=1500, cpu_seconds=30))(source)
    finally:
        stop.set()
        thread.join()
    elapsed = time.monotonic() - start
    assert report.status == "timeout" and report.signal == signal.SIGKILL
    assert 1.4 <= elapsed < CEILING_S
    assert ticks >= 10  # the host thread kept running throughout
    assert report.tree_clean
    # The host can immediately run another worker.
    assert worker()("def run(tools, payload):\n    return 'next'\n").result == "next"


def test_cpu_limit_kills_a_busy_loop_even_if_it_ignores_sigxcpu(worker: Any) -> None:
    source = (
        "import signal\n"
        "def run(tools, payload):\n"
        "    signal.signal(signal.SIGXCPU, signal.SIG_IGN)\n"
        "    while True:\n"
        "        pass\n"
    )
    report = worker(WorkerLimits(wall_ms=15_000, cpu_seconds=1))(source)
    assert report.status == "cpu_limit"
    assert report.cpu_ms >= 1000 and report.wall_ms < CEILING_S * 1000


@pytest.mark.parametrize(
    "body",
    [
        "return bytearray(1 << 30)",
        "blocks = []\n    while True:\n        blocks.append(bytearray(16 << 20))",
    ],
    ids=["single", "gradual"],
)
def test_memory_exhaustion_is_contained(worker: Any, body: str) -> None:
    source = f"def run(tools, payload):\n    {body}\n"
    report = worker(WorkerLimits(memory_bytes=256 << 20))(source)
    assert report.status in ("candidate_error", "crashed")
    if report.status == "candidate_error":
        assert report.error == "MemoryError"
    assert report.tree_clean


@pytest.mark.parametrize(
    "flood",
    [
        "print('x' * 10_000_000)",
        "while True:\n        sys.stdout.write('y' * 4096)",
        "while True:\n        os.write(2, b'z' * 4096)",
    ],
    ids=["stdout", "stdout-loop", "stderr-loop"],
)
def test_output_flood_is_cut_at_the_limit(worker: Any, flood: str) -> None:
    source = f"import os, sys\ndef run(tools, payload):\n    {flood}\n"
    limits = WorkerLimits(max_output_bytes=8192)
    report = worker(limits)(source)
    assert report.status == "output_limit"
    assert len(report.output) == 8192 and report.output_truncated


def test_scratch_file_flood_is_bounded_by_the_file_size_limit(worker: Any) -> None:
    source = candidate(
        """
f = open('big.bin', 'wb', buffering=0)
out['write'] = attempt(lambda: [f.write(b'0' * 65536) for _ in range(64)])
"""
    )
    report = worker(WorkerLimits(max_file_bytes=1 << 20))(source)
    assert report.status == "returned"
    assert report.result["write"] == "denied:OSError"  # EFBIG


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("os.write(FD, b'not json\\n')", "protocol_violation"),
        ("os.write(FD, b'[1, 2]\\n')", "protocol_violation"),
        (
            'os.write(FD, b\'{"type": "ready", "protocol": "uzumaki.broker/1.0"}\\n\')',
            "protocol_violation",
        ),
        ("os.write(FD, b'x' * 200000)", "protocol_violation"),
        ("[tools.call('observe') for _ in range(10)]", "tool_budget_exhausted"),
    ],
    ids=["not-json", "not-object", "second-ready", "oversized", "tool-budget"],
)
def test_broker_channel_abuse_stops_the_worker(worker: Any, body: str, status: str) -> None:
    source = (
        "import json, os, sys\n"
        "FD = json.loads(sys.argv[1])['request_fd']\n"
        "def run(tools, payload):\n"
        f"    {body}\n"
        "    import time\n"
        "    time.sleep(30)\n"
    )
    limits = WorkerLimits(wall_ms=15_000, max_message_bytes=4096, max_tool_calls=5)
    start = time.monotonic()
    report = worker(limits)(source)
    assert report.status == status
    assert time.monotonic() - start < 10


@pytest.mark.parametrize(
    ("body", "exit_code", "signum"),
    [
        ("os._exit(3)", 3, None),
        ("os.abort()", None, signal.SIGABRT),
        ("import ctypes; ctypes.string_at(0)", None, signal.SIGSEGV),
    ],
    ids=["exit", "abort", "segfault"],
)
def test_crashed_worker_is_reaped_and_the_host_continues(
    worker: Any, body: str, exit_code: int | None, signum: int | None
) -> None:
    report = worker()(f"import os\ndef run(tools, payload):\n    {body}\n")
    assert report.status == "crashed"
    assert (report.exit_code, report.signal) == (exit_code, signum)
    assert report.tree_clean
    assert worker()("def run(tools, payload):\n    return 1\n").status == "returned"


def test_candidate_errors_report_only_the_exception_type(worker: Any) -> None:
    report = worker()(f"def run(tools, payload):\n    raise RuntimeError({SECRET!r})\n")
    assert (report.status, report.error) == ("candidate_error", "RuntimeError")
    assert SECRET not in repr(report)
    report = worker()("def run(tools, payload):\n    return object()\n")
    assert (report.status, report.error) == ("candidate_error", "unserializable_result")
    report = worker()("x = 1\n")
    assert (report.status, report.error) == ("candidate_error", "TypeError")
    report = worker()("def run(tools, payload):\n    raise SystemExit(0)\n")
    assert (report.status, report.error) == ("candidate_error", "SystemExit")


def test_python_level_tricks_do_not_lift_kernel_restrictions(worker: Any) -> None:
    """Restoring builtins, re-importing modules or calling libc directly changes nothing."""
    source = candidate(
        """
import importlib, builtins
importlib.reload(socket)
libc = ctypes.CDLL('libc.so.6', use_errno=True)
out['libc_open'] = 'denied' if libc.open(b'/etc/passwd', 0) < 0 else 'allowed'
out['prctl_seccomp_off'] = attempt(lambda: libc.prctl(22, 0, 0, 0, 0) == 0 or 1 / 0)
out['socket'] = attempt(lambda: socket.socket())
out['open'] = attempt(lambda: builtins.open('/etc/hostname').read())
"""
    )
    report = worker()(source)
    assert report.status == "returned"
    assert report.result["libc_open"] == "denied"
    assert all(v.startswith("denied") for v in report.result.values()), report.result


def test_bootstrap_fails_closed_when_its_self_check_finds_a_reachable_canary(
    tmp_path: Path,
) -> None:
    """If the domain would allow the host canary, the candidate is never loaded."""
    from uzumaki import _sandbox_boot as boot

    exposed = tmp_path / "exposed"
    exposed.mkdir()
    canary = exposed / "canary"
    canary.write_text("canary", encoding="ascii")
    marker = tmp_path / "candidate-ran"
    (tmp_path / "candidate.py").write_text(
        f"open({str(marker)!r}, 'w').close()\ndef run(tools, payload):\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "payload.json").write_text("null", encoding="utf-8")
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    config = {
        "protocol": "uzumaki.broker/1.0",
        "request_fd": req_w,
        "response_fd": resp_r,
        "candidate": str(tmp_path / "candidate.py"),
        "payload": str(tmp_path / "payload.json"),
        "scratch": str(tmp_path / "scratch"),
        "read_paths": [str(exposed)],  # misconfigured: the canary is inside an allowed path
        "probe_paths": [str(canary)],
        "tools": {},
        "max_message_bytes": 4096,
        "limits": {
            "cpu_seconds": 5,
            "memory_bytes": 512 << 20,
            "max_file_bytes": 1 << 20,
            "max_open_files": 64,
        },
    }
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", boot.__file__, json.dumps(config)],
        pass_fds=(req_w, resp_r),
        env={},
        start_new_session=True,
    )
    os.close(req_w)
    os.close(resp_r)
    try:
        assert proc.wait(timeout=CEILING_S) == boot.CONTAINMENT_EXIT
        message = os.read(req_r, 4096)
    finally:
        os.close(req_r)
        os.close(resp_w)
    assert json.loads(message) == {
        "type": "containment_failed",
        "reason": "filesystem_not_restricted",
    }
    assert not marker.exists()
