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
