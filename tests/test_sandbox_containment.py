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


def metadata_tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Mode, owner, mtime, ctime and xattr names of every entry, which content hashes miss."""
    snapshot: dict[str, tuple[Any, ...]] = {}
    for p in sorted([root, *root.rglob("*")]):
        st = os.lstat(p)
        try:
            xattrs: list[str] | None = sorted(os.listxattr(p, follow_symlinks=False))
        except OSError:
            xattrs = None
        snapshot[str(p.relative_to(root))] = (
            st.st_mode,
            st.st_uid,
            st.st_gid,
            st.st_mtime_ns,
            st.st_ctime_ns,
            xattrs,
        )
    return snapshot


def test_metadata_of_protected_and_readable_files_cannot_be_changed(
    worker: Any, tmp_path: Path
) -> None:
    """Landlock does not cover chmod, chown, utime or xattrs, so seccomp denies them."""
    assets = tmp_path / "assets"
    for name in ("evaluator", "prompts", "skills", "heldout"):
        (assets / name).mkdir(parents=True)
        (assets / name / "data.json").write_text('{"answer": 42}', encoding="utf-8")
    evaluator = str(assets / "evaluator" / "data.json")
    heldout = str(assets / "heldout" / "data.json")
    try:
        os.setxattr(heldout, "user.uzumaki", b"1")
    except OSError:
        pass  # the host filesystem may not support user xattrs; the removal attempt still runs
    before = metadata_tree(assets)
    before_content = digest_tree(assets)
    uid, gid = os.getuid(), os.getgid()
    # Every attempt must fail with EPERM (errno 1, the seccomp filter), not EACCES from Landlock.
    # Readable files are re-chmodded to their current mode so a regression cannot alter them.
    source = candidate(
        f"""
libc = ctypes.CDLL(None, use_errno=True)
def raw(nr, *args):
    rc = libc.syscall(nr, *args)
    return 'denied:errno%d' % ctypes.get_errno() if rc < 0 else 'allowed:%d' % rc
def errno_of(fn):
    try:
        return 'allowed:' + repr(fn())[:100]
    except OSError as error:
        return 'denied:errno%d' % error.errno
def same_mode(path):
    return os.stat(path).st_mode & 0o7777
out['chmod_evaluator'] = errno_of(lambda: os.chmod({evaluator!r}, 0))
out['chmod_heldout_wide'] = errno_of(lambda: os.chmod({heldout!r}, 0o666))
out['chmod_dir'] = errno_of(lambda: os.chmod({str(assets / "skills")!r}, 0o777))
out['chown_same_owner'] = errno_of(lambda: os.chown({evaluator!r}, {uid}, {gid}))
out['lchown'] = errno_of(lambda: os.lchown({evaluator!r}, {uid}, {gid}))
out['utime'] = errno_of(lambda: os.utime({evaluator!r}, (0, 0)))
out['utime_now'] = errno_of(lambda: os.utime({heldout!r}))
out['setxattr'] = errno_of(lambda: os.setxattr({evaluator!r}, 'user.x', b'1'))
out['removexattr'] = errno_of(lambda: os.removexattr({heldout!r}, 'user.uzumaki'))
out['raw_chmod'] = raw(90, {evaluator.encode()!r}, 0)
out['raw_fchmodat'] = raw(268, -100, {evaluator.encode()!r}, 0)
out['raw_fchmodat2'] = raw(452, -100, {evaluator.encode()!r}, 0, 0)
out['raw_fchownat'] = raw(260, -100, {evaluator.encode()!r}, {uid}, {gid}, 0)
out['raw_utimes'] = raw(235, {evaluator.encode()!r}, None)
out['raw_utimensat'] = raw(280, -100, {evaluator.encode()!r}, None, 0)
out['raw_futimesat'] = raw(261, -100, {evaluator.encode()!r}, None)
out['raw_lsetxattr'] = raw(189, {evaluator.encode()!r}, b'user.x', b'1', 1, 0)
out['raw_setxattrat'] = raw(463, -100, {evaluator.encode()!r}, 0, b'user.x', None, 0)
out['raw_removexattrat'] = raw(466, -100, {heldout.encode()!r}, 0, b'user.uzumaki')
own = CONFIG['candidate']
out['chmod_candidate'] = errno_of(lambda: os.chmod(own, same_mode(own)))
out['chmod_boot'] = errno_of(lambda: os.chmod(sys.argv[0], same_mode(sys.argv[0])))
scratch = open('mine.txt', 'w')
out['fchmod_scratch'] = errno_of(lambda: os.fchmod(scratch.fileno(), 0o777))
out['fchown_scratch'] = errno_of(lambda: os.fchown(scratch.fileno(), {uid}, {gid}))
out['futime_scratch'] = errno_of(lambda: os.utime(scratch.fileno(), (0, 0)))
out['fsetxattr_scratch'] = errno_of(lambda: os.setxattr(scratch.fileno(), 'user.x', b'1'))
out['getxattr'] = errno_of(lambda: os.getxattr({heldout!r}, 'user.uzumaki'))
out['listxattr'] = errno_of(lambda: os.listxattr({heldout!r}))
out['inotify_init1'] = raw(294, 0)
out['inotify_add_watch'] = raw(254, 0, CONFIG['candidate'].encode(), 0x2)
out['fanotify_init'] = raw(300, 0, 0)
"""
    )
    report = worker(protected=(assets,))(source)
    assert report.status == "returned"
    assert len(report.result) == 30
    assert {k: v for k, v in report.result.items() if v != "denied:errno1"} == {}
    assert metadata_tree(assets) == before
    assert digest_tree(assets) == before_content


# --- persistent kernel IPC ----------------------------------------------------------------------


def sysv_keys(kind: str) -> set[int]:
    """Keys of SysV IPC objects of one kind (msg, shm, sem) in the host IPC namespace."""
    try:
        lines = Path(f"/proc/sysvipc/{kind}").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return set()
    return {int(line.split()[0]) for line in lines if line.strip()}


def test_sysv_ipc_and_posix_queues_are_denied_and_leave_no_object(worker: Any) -> None:
    """IPC objects outlive processes, so a candidate must not be able to create or reach them."""
    key = 0x55A00000 | (os.getpid() & 0xFFFF)
    queue = f"uzumaki-test-{os.getpid()}".encode()
    for kind in ("msg", "shm", "sem"):
        assert key not in sysv_keys(kind)
    source = candidate(
        f"""
libc = ctypes.CDLL(None, use_errno=True)
def raw(nr, *args):
    rc = libc.syscall(nr, *args)
    return 'denied:errno%d' % ctypes.get_errno() if rc < 0 else 'allowed:%d' % rc
CREAT = 0o1000 | 0o600
out['msgget'] = raw(68, {key}, CREAT)
out['msgget_private'] = raw(68, 0, CREAT)
out['shmget'] = raw(29, {key}, 4096, CREAT)
out['semget'] = raw(64, {key}, 1, CREAT)
out['msgsnd'] = raw(69, 0, b'\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00hi', 2, 0)
out['msgrcv'] = raw(70, 0, ctypes.create_string_buffer(64), 32, 0, 0o4000)
out['shmat'] = raw(30, 0, None, 0)
out['semop'] = raw(65, 0, None, 0)
out['msgctl'] = raw(71, 0, 2, None)
out['shmctl'] = raw(31, 0, 2, None)
out['semctl'] = raw(66, 0, 0, 2, None)
out['mq_open'] = raw(240, {queue!r}, 0o100 | 0o2, 0o600, None)
out['mq_unlink'] = raw(241, {queue!r})
"""
    )
    report = worker()(source)
    assert report.status == "returned"
    assert {k: v for k, v in report.result.items() if v != "denied:errno1"} == {}
    assert report.tree_clean
    # Nothing survives the run in the shared IPC namespace.
    for kind in ("msg", "shm", "sem"):
        assert key not in sysv_keys(kind)
    mqueue = Path("/dev/mqueue")
    if mqueue.is_dir():
        assert not (mqueue / queue.decode()).exists()


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


def scheduling_state(pid: int) -> tuple[Any, ...]:
    """Nice value, policy, static priority, CPU affinity and I/O priority of a host process."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return (
        os.getpriority(os.PRIO_PROCESS, pid),
        os.sched_getscheduler(pid),
        os.sched_getparam(pid).sched_priority,
        frozenset(os.sched_getaffinity(pid)),
        libc.syscall(252, 1, pid),  # ioprio_get(IOPRIO_WHO_PROCESS, pid)
    )


def test_host_process_priority_scheduling_and_affinity_cannot_be_changed(worker: Any) -> None:
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    parent = os.getpid()  # the process that owns the watchdog and kill authority
    try:
        before = {"victim": scheduling_state(victim.pid), "parent": scheduling_state(parent)}
        p_nice, p_policy, _, p_cpus, p_ioprio = before["parent"]
        p_mask = sum(1 << cpu for cpu in p_cpus)
        cpu = min(before["victim"][3])
        nobody = 0x7FFFFFF0  # a UID/PGID with no processes: unfiltered, these return ESRCH
        # Against the parent, the values are its current ones, so a regression changes nothing
        # on the host; against the disposable victim, they are the real attack values.
        source = candidate(
            f"""
import struct
libc = ctypes.CDLL(None, use_errno=True)
def raw(nr, *args):
    ctypes.set_errno(0)
    rc = libc.syscall(nr, *args)
    return 'denied:errno%d' % ctypes.get_errno() if rc < 0 else 'allowed:%d' % rc
def attr(policy, nice):  # struct sched_attr, version 1 (48 bytes)
    packed = struct.pack('<IIQiIQQQ', 48, policy, 0, nice, 0, 0, 0, 0)
    return ctypes.create_string_buffer(packed, 48)
def mask(bits):
    return ctypes.create_string_buffer(bits.to_bytes(128, 'little'), 128)
param = ctypes.c_int(0)
out['renice_user'] = raw(141, 2, {nobody}, 19)
out['renice_pgrp'] = raw(141, 1, {nobody}, 19)
out['renice_self'] = raw(141, 0, 0, 19)
out['renice_victim'] = raw(141, 0, {victim.pid}, 19)
out['renice_parent'] = raw(141, 0, {parent}, {p_nice})
out['setparam_victim'] = raw(142, {victim.pid}, ctypes.byref(param))
out['setparam_parent'] = raw(142, {parent}, ctypes.byref(param))
out['scheduler_victim'] = raw(144, {victim.pid}, 5, ctypes.byref(param))  # SCHED_IDLE
out['scheduler_parent'] = raw(144, {parent}, {p_policy}, ctypes.byref(param))
out['affinity_victim'] = raw(203, {victim.pid}, 128, mask(1 << {cpu}))
out['affinity_parent'] = raw(203, {parent}, 128, mask({p_mask}))
out['ioprio_victim'] = raw(251, 1, {victim.pid}, 3 << 13)  # IOPRIO_CLASS_IDLE
out['ioprio_parent'] = raw(251, 1, {parent}, {p_ioprio})
out['ioprio_user'] = raw(251, 3, {nobody}, 3 << 13)
out['setattr_victim'] = raw(314, {victim.pid}, attr(5, 19), 0)
out['setattr_parent'] = raw(314, {parent}, attr({p_policy}, {p_nice}), 0)
out['lib_renice'] = attempt(lambda: os.setpriority(os.PRIO_PROCESS, {victim.pid}, 19))
out['lib_affinity'] = attempt(lambda: os.sched_setaffinity({victim.pid}, {{{cpu}}}))
idle = os.sched_param(0)
out['lib_scheduler'] = attempt(lambda: os.sched_setscheduler({victim.pid}, os.SCHED_IDLE, idle))
"""
        )
        report = worker()(source)
        assert report.status == "returned", report
        assert report.tree_clean
        names = ("lib_renice", "lib_affinity", "lib_scheduler")
        library = {name: report.result.pop(name) for name in names}
        assert library == dict.fromkeys(names, "denied:PermissionError")
        # errno 1 for every call, including the ones that would otherwise fail with ESRCH: the
        # denial comes from the seccomp filter, not from the kernel's same-UID check.
        assert report.result == dict.fromkeys(report.result, "denied:errno1")
        assert len(report.result) == 16
        assert scheduling_state(victim.pid) == before["victim"]
        assert scheduling_state(parent) == before["parent"]
        assert victim.poll() is None
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
