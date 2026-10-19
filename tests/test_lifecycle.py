import json
from pathlib import Path

import pytest

from fakes import FakeClock
from uzumaki.lifecycle import (
    STATES,
    STATUS_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    Lifecycle,
    LifecycleError,
)

CONTRACT = Path(__file__).parents[1] / "docs" / "contracts" / "v1.json"


def test_states_and_edges_match_the_frozen_contract() -> None:
    lifecycle = json.loads(CONTRACT.read_text(encoding="utf-8"))["lifecycle"]
    assert list(STATES) == lifecycle["states"]
    assert TERMINAL_STATES == set(lifecycle["terminal"])
    assert TRANSITIONS == {(t["from"], t["to"]): t["on"] for t in lifecycle["transitions"]}
    assert set(STATUS_STATES.values()) <= TERMINAL_STATES
    # A status that shares its name with a protocol primary outcome lands in a lifecycle state
    # that allows that primary.
    allowed = lifecycle["lifecycle_to_primary"]
    primaries = {p for names in allowed.values() for p in names}
    for status, state in STATUS_STATES.items():
        if status in primaries:
            assert status in allowed[state]


def started() -> Lifecycle:
    life = Lifecycle(FakeClock())
    assert life.start().applied
    return life


@pytest.mark.parametrize(
    ("status", "progress"),
    [
        ("completed", "goal_reached"),
        ("abstained", "none"),
        ("blocked", "none"),
        ("partial_progress", "partial"),
        ("timeout", "partial"),
        ("budget_exhausted", "none"),
        ("failed", "partial"),
    ],
)
def test_each_status_is_terminal_and_idempotent(status: str, progress: str) -> None:
    life = started()
    life.record_progress(progress)
    first = life.finish(status, "first")
    assert first.applied and life.state == STATUS_STATES[status] and life.terminal
    repeat = life.finish(status, "again")
    assert not repeat.applied and repeat.diagnostic == "terminal_repeat_ignored"
    other = life.finish("failed" if status != "failed" else "timeout")
    assert not other.applied and other.diagnostic == "lifecycle_violation"
    assert not life.start().applied and life.cancel().diagnostic == "lifecycle_violation"
    assert (life.status, life.detail, life.progress) == (status, "first", progress)
    assert len([h for h in life.history if h["to"] in TERMINAL_STATES]) == 1


def test_cancellation_goes_through_cancelling_and_repeats_are_ignored() -> None:
    life = started()
    assert life.cancel().applied and life.state == "cancelling"
    assert life.cancel().diagnostic == "repeat_ignored"
    # No voluntary or budget stop may overtake an accepted cancel; only cancelled or failed.
    for status in ("completed", "abstained", "timeout", "budget_exhausted"):
        assert life.finish(status).diagnostic == "lifecycle_violation"
    assert life.finish("cancelled").applied and life.state == "cancelled"
    assert life.cancel().diagnostic == "terminal_repeat_ignored"
    assert life.finish("cancelled").diagnostic == "terminal_repeat_ignored"
    assert [h["on"] for h in life.history] == [
        "admitted_and_reset_ready",
        "cancel_requested",
        "safe_stop_reported_and_evaluated",
    ]


def test_queued_run_can_be_cancelled_rejected_or_failed_without_running() -> None:
    life = Lifecycle(FakeClock())
    assert life.cancel().applied and life.state == "cancelled" and life.status == "cancelled"
    assert life.history[-1]["on"] == "cancel_before_start"
    rejected = Lifecycle(FakeClock())
    assert rejected.finish("rejected").applied and rejected.progress == "none"
    assert rejected.finish("rejected").diagnostic == "terminal_repeat_ignored"
    failed = Lifecycle(FakeClock())
    assert failed.finish("failed", "reset").applied
    assert failed.history[-1]["on"] == "reset_or_readiness_failure"
    for status in ("completed", "timeout", "cancelled"):
        assert Lifecycle(FakeClock()).finish(status).diagnostic == "lifecycle_violation"


def test_running_cannot_be_rejected_or_cancelled_without_cancelling() -> None:
    life = started()
    assert life.finish("rejected").diagnostic == "lifecycle_violation"
    assert life.finish("cancelled").diagnostic == "lifecycle_violation"
    assert life.start().diagnostic == "repeat_ignored"
    assert life.state == "running"


def test_completion_and_partial_progress_require_matching_observed_progress() -> None:
    life = started()
    assert life.finish("completed").diagnostic == "lifecycle_violation"
    assert life.finish("partial_progress").diagnostic == "lifecycle_violation"
    life.record_progress("partial")
    assert life.finish("completed").diagnostic == "lifecycle_violation"
    assert life.finish("partial_progress").applied


def test_progress_never_decreases_and_is_not_rewritten_after_terminal() -> None:
    life = started()
    life.record_progress("partial")
    life.record_progress("none")
    assert life.progress == "partial"
    assert life.cancel().applied
    assert life.finish("cancelled").applied
    life.record_progress("goal_reached", "late_sensor")
    assert life.progress == "partial"  # the cancelled outcome is not rewritten
    assert life.post_terminal == [
        {"progress": "goal_reached", "source": "late_sensor", "at_ms": 1000}
    ]


def test_unknown_values_are_caller_errors() -> None:
    life = started()
    with pytest.raises(LifecycleError):
        life.finish("succeeded")
    with pytest.raises(LifecycleError):
        life.record_progress("half")
