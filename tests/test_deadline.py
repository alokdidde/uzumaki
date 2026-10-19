"""Real-process deadline and cleanup tests (real clock, real child processes, POSIX).

Timing bounds are generous ceilings for slow CI hosts, not latency claims.
"""

import os
import time
from pathlib import Path
from typing import Any

import pytest

import process_workers as workers
from fakes import budgets
from uzumaki.deadline import ProcessProvider, run_in_process
from uzumaki.faults import ProviderTimeout, WorkerFailure
from uzumaki.supervisor import BoundedRun

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

CEILING_S = 20.0


def gone(pid: int) -> bool:
    """True when ``pid`` no longer runs (absent or a zombie awaiting an unrelated reaper)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rsplit(")", 1)[1].split()[0] in ("Z", "X")


def wait_gone(pid: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if gone(pid):
            return True
        time.sleep(0.05)
    return gone(pid)


def wait_pid_file(path: Path) -> int:
    assert path.exists(), "worker did not record its grandchild"
    return int(path.read_text(encoding="ascii"))


def test_result_within_deadline() -> None:
    assert run_in_process(workers.answer, {"n": 4}, timeout_ms=15_000, max_output_bytes=1024) == {
        "decision": "abstain",
        "echo": 4,
    }


def test_hung_call_is_killed_at_the_deadline_and_parent_stays_responsive() -> None:
    start = time.monotonic()
    with pytest.raises(ProviderTimeout):
        run_in_process(workers.hang, {}, timeout_ms=1500, max_output_bytes=1024)
    elapsed = time.monotonic() - start
    assert 1.4 <= elapsed < CEILING_S


def test_deadline_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(ProviderTimeout):
        run_in_process(
            workers.hang_with_grandchild,
            {"pid_file": str(pid_file)},
            timeout_ms=4000,
            max_output_bytes=1024,
        )
    assert wait_gone(wait_pid_file(pid_file))


def test_descendants_are_cleaned_up_after_a_normal_reply(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    result = run_in_process(
        workers.leave_grandchild,
        {"pid_file": str(pid_file)},
        timeout_ms=15_000,
        max_output_bytes=1024,
    )
    assert result == {"decision": "abstain"}
    assert wait_gone(wait_pid_file(pid_file))


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (workers.crash, "worker_exited"),
        (workers.raise_error, "worker_exception"),
        (workers.flood, "oversized_output"),
        (workers.not_json, "worker_exception"),
    ],
    ids=["crash", "exception", "flood", "not_json"],
)
def test_worker_failures_are_bounded_and_do_not_echo_content(target: Any, reason: str) -> None:
    start = time.monotonic()
    with pytest.raises(WorkerFailure) as exc:
        run_in_process(target, {}, timeout_ms=15_000, max_output_bytes=4096)
    assert str(exc.value) == reason and "secret" not in str(exc.value)
    assert time.monotonic() - start < CEILING_S


def test_process_provider_rejects_non_object_results() -> None:
    provider = ProcessProvider(workers.not_object, timeout_ms=15_000, max_output_bytes=1024)
    with pytest.raises(WorkerFailure):
        provider.request({})


def test_bounded_run_times_out_a_hung_real_provider_without_further_calls() -> None:
    provider = ProcessProvider(workers.hang, timeout_ms=60_000, max_output_bytes=1024)
    run = BoundedRun(
        run_id="real-1",
        budgets=budgets(wall_ms=2000, retries=50, model_calls=50),
        provider=provider,
    )
    released: list[str] = []
    run.add_resource("marker", lambda: released.append("marker"))
    start = time.monotonic()
    report = run.execute(lambda r: (r.call_model("planner", {}), "completed")[1])
    elapsed = time.monotonic() - start
    assert (report.state, report.status, report.detail) == ("completed", "timeout", "wall_time")
    assert 1.9 <= elapsed < CEILING_S
    assert provider.calls == 1  # the deadline consumed the whole wall budget: no retry storm
    assert released == ["marker"]


def test_bounded_run_retries_a_real_timeout_then_succeeds() -> None:
    class Flaky(ProcessProvider):
        def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
            if self.calls == 0:
                self._target = workers.hang
            else:
                self._target = workers.answer
            return super().request_within(request, timeout_ms)

    provider = Flaky(workers.answer, timeout_ms=60_000, max_output_bytes=1024)
    run = BoundedRun(
        run_id="real-2",
        budgets=budgets(wall_ms=60_000, model_call_timeout_ms=1500),
        provider=provider,
    )
    report = run.execute(lambda r: (r.call_model("planner", {"n": 1}), "abstained")[1])
    assert report.status == "abstained" and provider.calls == 2
    assert report.usage["retries"]["used"] == 1
