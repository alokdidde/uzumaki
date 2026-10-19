"""Admission: unverified platforms and exposed protected paths are refused before any process."""

import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

import pytest

from uzumaki import sandbox
from uzumaki.broker import pick_place_v1_broker
from uzumaki.sandbox import (
    MIN_LANDLOCK_ABI,
    ContainmentUnavailable,
    PlatformReport,
    RestrictedWorker,
    WorkerLimits,
    admit,
    probe_platform,
)

VERIFIED = PlatformReport("Linux", "x86_64", "6.17.0", 7, True, "3.12.12")
SOURCE = "def run(tools, payload):\n    return 1\n"


def report(**changes: Any) -> PlatformReport:
    return PlatformReport(**{**VERIFIED.as_dict(), **changes})


def no_popen(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a worker process was started")


@pytest.mark.parametrize(
    ("platform", "reason"),
    [
        (report(system="Darwin"), "unsupported_os"),
        (report(system="Windows", machine="AMD64"), "unsupported_os"),
        (report(machine="aarch64"), "unverified_architecture"),
        (report(landlock_abi=MIN_LANDLOCK_ABI - 1), "landlock_abi_too_old"),
        (report(landlock_abi=0), "landlock_abi_too_old"),
        (report(seccomp_filter=False), "seccomp_unavailable"),
    ],
)
def test_unverified_platforms_fail_admission_without_starting_a_worker(
    platform: PlatformReport, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ContainmentUnavailable, match=reason):
        admit(platform)
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    worker = RestrictedWorker(pick_place_v1_broker({}), probe=lambda: platform)
    with pytest.raises(ContainmentUnavailable, match=reason):
        worker.run(SOURCE)


def test_verified_platform_is_admitted() -> None:
    assert admit(VERIFIED) is VERIFIED


@pytest.mark.parametrize("which", ["work_root", "system_libraries", "stdlib", "root"])
def test_protected_paths_that_the_worker_could_reach_are_refused(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = {
        "work_root": tmp_path,
        "system_libraries": Path("/usr/lib"),
        "stdlib": Path(sysconfig.get_paths()["stdlib"]) / "os.py",
        "root": Path("/"),
    }[which]
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    worker = RestrictedWorker(
        pick_place_v1_broker({}),
        protected_paths=[protected],
        probe=lambda: VERIFIED,
        work_root=tmp_path,
    )
    with pytest.raises(ContainmentUnavailable, match="protected_path_exposed"):
        worker.run(SOURCE)
    assert list(tmp_path.glob("uzumaki-*")) == []  # the work directory is removed


@pytest.mark.parametrize(
    "changes",
    [
        {"wall_ms": 0},
        {"cpu_seconds": -1},
        {"memory_bytes": 1024},
        {"max_output_bytes": 1.5},
        {"max_tool_calls": True},
        {"max_open_files": 4},
    ],
)
def test_invalid_limits_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WorkerLimits(**changes)


def test_bad_inputs_are_refused_before_any_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    worker = RestrictedWorker(pick_place_v1_broker({}), probe=lambda: VERIFIED)
    with pytest.raises(ValueError):
        worker.run(b"def run(t, p): pass")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        worker.run(SOURCE, {"x": float("nan")})
    with pytest.raises(ValueError):
        RestrictedWorker(object())  # type: ignore[arg-type]


def test_probe_reports_this_host_without_side_effects() -> None:
    probe = probe_platform()
    assert probe.python == ".".join(map(str, sys.version_info[:3]))
    assert probe.landlock_abi >= 0


def test_host_and_bootstrap_agree_on_the_minimum_landlock_abi() -> None:
    source = (Path(sandbox.__file__).with_name("_sandbox_boot.py")).read_text(encoding="utf-8")
    assert f"MIN_LANDLOCK_ABI = {MIN_LANDLOCK_ABI}\n" in source


# x86_64 numbers Landlock cannot mediate: metadata changes, xattrs, watches, persistent IPC and
# scheduling changes to other same-UID processes.
# fmt: off
UNMEDIATED_BY_LANDLOCK = {
    "chmod": 90, "fchmod": 91, "chown": 92, "fchown": 93, "lchown": 94, "utime": 132,
    "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190, "removexattr": 197,
    "lremovexattr": 198, "fremovexattr": 199, "utimes": 235, "fchownat": 260,
    "futimesat": 261, "fchmodat": 268, "utimensat": 280, "fchmodat2": 452,
    "setxattrat": 463, "removexattrat": 466, "file_setattr": 469,
    "getxattr": 191, "lgetxattr": 192, "fgetxattr": 193, "listxattr": 194, "llistxattr": 195,
    "flistxattr": 196, "getxattrat": 464, "listxattrat": 465,
    "inotify_init": 253, "inotify_add_watch": 254, "inotify_init1": 294,
    "fanotify_init": 300, "fanotify_mark": 301,
    "shmget": 29, "shmat": 30, "shmctl": 31, "semget": 64, "semop": 65, "semctl": 66,
    "shmdt": 67, "msgget": 68, "msgsnd": 69, "msgrcv": 70, "msgctl": 71, "semtimedop": 220,
    "mq_open": 240, "mq_unlink": 241, "mq_timedsend": 242, "mq_timedreceive": 243,
    "mq_notify": 244, "mq_getsetattr": 245,
    # Same-UID checks only (no ptrace hook), so Landlock's ptrace and signal scoping miss them.
    "setpriority": 141, "sched_setparam": 142, "sched_setscheduler": 144,
    "sched_setaffinity": 203, "ioprio_set": 251, "sched_setattr": 314,
}
# fmt: on


@pytest.mark.skipif(sys.platform != "linux", reason="the bootstrap loads the Linux libc")
def test_seccomp_program_denies_syscalls_landlock_cannot_mediate() -> None:
    """Decode the assembled filter: each number must jump to the EPERM return, not ALLOW."""
    import struct

    from uzumaki import _sandbox_boot as boot

    program = boot._seccomp_program()
    code = [struct.unpack("<HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    eperm = boot._RET_ERRNO | boot._EPERM
    denied = {
        k
        for index, (op, jt, _jf, k) in enumerate(code)
        if op == 0x15 and code[index + 1 + jt] == (0x06, 0, 0, eperm)
    }
    for name, nr in UNMEDIATED_BY_LANDLOCK.items():
        assert boot._SYS[name] == nr, name
        assert nr in denied, name
