"""Fault injection for BoundedRun with a fake clock: every scenario must terminate finitely and
make no provider or tool call after the run leaves ``running``."""

import threading
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeClock, ScriptedProvider, ScriptedTools, budgets, fail, ok
from uzumaki.budgets import Budgets
from uzumaki.evidence import ToolResult, sha256_of
from uzumaki.faults import ProviderTimeout, TransientProviderError, WorkerFailure
from uzumaki.providers import RecordedProvider
from uzumaki.supervisor import BoundedRun, ResultFenced, RunStopped

SELECT = {"decision": "select", "skill_id": "pick-cube"}


def new_run(
    provider: Any = None,
    tools: Any = None,
    clock: FakeClock | None = None,
    run_id: str = "run-1",
    **limits: Any,
) -> tuple[BoundedRun, FakeClock]:
    clock = clock or FakeClock()
    b: Budgets = budgets(**limits)
    run = BoundedRun(run_id=run_id, budgets=b, provider=provider, tools=tools, clock=clock)
    if isinstance(tools, ScriptedTools):
        tools.observer = run.record_progress  # the fake environment reports what it observed
    return run, clock


def assert_no_more_calls(run: BoundedRun, provider: Any, tools: Any = None) -> None:
    """After a terminal state nothing reaches the provider or the tools."""
    model_calls = provider.calls if provider is not None else 0
    tool_calls = len(tools.calls) if tools is not None else 0
    if provider is not None:
        with pytest.raises(RunStopped):
            run.call_model("planner", {"again": True})
    if tools is not None:
        with pytest.raises(RunStopped):
            run.call_tool("pick", {})
    with pytest.raises(RunStopped):
        run.checkpoint()
    with pytest.raises(RunStopped):
        run.ticket("model")
    with pytest.raises(RunStopped):
        run.new_attempt()
    assert (provider.calls if provider is not None else 0) == model_calls
    assert (len(tools.calls) if tools is not None else 0) == tool_calls


def toy_logic(run: BoundedRun) -> str:
    decision = run.call_model("planner", {"role": "planner", "observation_id": "obs-1"})
    if decision.get("decision") != "select":
        return "abstained"
    # Progress comes from the environment (ScriptedTools), never from this logic.
    run.call_tool("pick", {"target": "cube"}, skill_id=decision["skill_id"])
    run.call_tool("place", {"x_m": 0.1})
    return "completed"


# ----------------------------------------------------------------------------- normal paths


def test_completed_run_reports_status_progress_and_usage() -> None:
    provider = ScriptedProvider([ok({**SELECT, "usage": {"input_tokens": 5, "output_tokens": 2}})])
    tools = ScriptedTools()
    run, _ = new_run(provider, tools)
    report = run.execute(toy_logic)
    assert (report.state, report.status, report.progress) == (
        "completed",
        "completed",
        "goal_reached",
    )
    assert report.usage["model_calls"]["used"] == 1 and report.usage["steps"]["used"] == 2
    assert report.usage["tokens"]["total"] == 7 and report.usage["cost_micro_usd"]["total"] is None
    assert report.fenced == [] and report.attempt_id == "a1"
    assert [c[1] for c in tools.calls] == ["pick", "place"]
    assert_no_more_calls(run, provider, tools)


def test_recorded_provider_keeps_working_through_the_bounded_run() -> None:
    provider = RecordedProvider.from_path(
        Path(__file__).parent / "fixtures" / "recorded_provider.json"
    )
    run, _ = new_run(provider)
    report = run.execute(
        lambda r: "abstained"
        if r.call_model("planner", {"role": "planner", "input": "fixture"})["decision"] == "abstain"
        else "completed"
    )
    assert (report.state, report.status) == ("completed", "abstained")


def test_unknown_request_to_recorded_provider_fails_without_retry() -> None:
    provider = RecordedProvider.from_path(
        Path(__file__).parent / "fixtures" / "recorded_provider.json"
    )
    run, _ = new_run(provider)
    report = run.execute(lambda r: (r.call_model("planner", {"role": "x"}), "completed")[1])
    assert (report.state, report.detail) == ("failed", "provider_error")
    assert report.usage["model_calls"]["used"] == 1 and report.usage["retries"]["used"] == 0


# ------------------------------------------------------------------- provider fault injection


def test_provider_timeout_is_retried_within_budget() -> None:
    clock = FakeClock()
    provider = ScriptedProvider([fail(ProviderTimeout("x"), clock, 5000), ok(SELECT)])
    run, _ = new_run(provider, ScriptedTools(), clock, model_call_timeout_ms=10_000)
    report = run.execute(toy_logic)
    assert report.status == "completed" and provider.calls == 2
    assert report.usage["retries"]["used"] == 1
    assert [d["cause"] for d in report.diagnostics if d["kind"] == "retry"] == ["provider_timeout"]


def test_late_completion_after_call_deadline_is_fenced_and_not_used() -> None:
    clock = FakeClock()
    late = {"decision": "select", "skill_id": "late-skill"}
    provider = ScriptedProvider([ok(late, clock, 3001), ok({"decision": "abstain"})])
    run, _ = new_run(provider, ScriptedTools(), clock, model_call_timeout_ms=3000)
    report = run.execute(toy_logic)
    assert report.status == "abstained"  # the late selection was never applied
    assert len(report.fenced) == 1
    fenced = report.fenced[0]
    assert (fenced["reason"], fenced["call_id"], fenced["attempt_id"]) == (
        "deadline_exceeded",
        "m1",
        "a1",
    )
    assert fenced["payload_sha256"] == sha256_of(late) and "late-skill" not in str(report.fenced)


def test_retry_storm_stops_at_the_retry_budget() -> None:
    provider = ScriptedProvider([fail(TransientProviderError("429"))], repeat_last=True)
    run, _ = new_run(provider, retries=4)
    report = run.execute(toy_logic)
    assert (report.status, report.detail) == ("budget_exhausted", "retries")
    assert provider.calls == 5  # first try plus 4 retries, then nothing
    assert_no_more_calls(run, provider)


def test_retry_storm_stops_at_the_model_call_budget() -> None:
    provider = ScriptedProvider([fail(TransientProviderError("disconnect"))], repeat_last=True)
    run, _ = new_run(provider, retries=1000, model_calls=6)
    report = run.execute(toy_logic)
    assert (report.status, report.detail, provider.calls) == ("budget_exhausted", "model_calls", 6)


def test_retry_storm_stops_at_wall_time() -> None:
    clock = FakeClock()
    provider = ScriptedProvider([fail(ProviderTimeout("slow"), clock, 10_000)], repeat_last=True)
    run, _ = new_run(provider, clock=clock, retries=1000, wall_ms=25_000)
    report = run.execute(toy_logic)
    assert (report.state, report.status, report.detail) == ("completed", "timeout", "wall_time")
    assert provider.calls == 3


@pytest.mark.parametrize(
    "bad",
    [
        ["not", "an", "object"],
        {"value": float("nan")},
        {"blob": "x" * 5000},
        {"usage": {"input_tokens": -3, "output_tokens": 1}},
        {"decision": "unexpected"},
    ],
    ids=["non_object", "non_json", "oversized", "bad_usage", "schema"],
)
def test_malformed_results_are_retried_then_exhaust_retries(bad: Any) -> None:
    provider = ScriptedProvider([ok(bad)], repeat_last=True)
    run, _ = new_run(provider, retries=2)

    def logic(r: BoundedRun) -> str:
        r.call_model(
            "planner", {}, validate=lambda out: out.get("decision") in ("select", "abstain")
        )
        return "abstained"

    report = run.execute(logic)
    assert (report.status, report.detail, provider.calls) == ("budget_exhausted", "retries", 3)
    assert {d["cause"] for d in report.diagnostics if d["kind"] == "retry"} == {"invalid_output"}


def test_malformed_then_valid_result_recovers() -> None:
    provider = ScriptedProvider([ok({"decision": 7}), ok({"decision": "abstain"})])
    run, _ = new_run(provider)
    report = run.execute(
        lambda r: (
            r.call_model("planner", {}, validate=lambda o: isinstance(o["decision"], str)),
            "abstained",
        )[1]
    )
    assert report.status == "abstained" and report.usage["retries"]["used"] == 1


def test_worker_failure_fails_the_run_without_retry() -> None:
    provider = ScriptedProvider([fail(WorkerFailure("crash")), ok(SELECT)])
    run, _ = new_run(provider)
    report = run.execute(toy_logic)
    assert (report.state, report.status, report.detail) == ("failed", "failed", "worker_failure")
    assert provider.calls == 1
    assert_no_more_calls(run, provider)


# ---------------------------------------------------------------------- budgets on tools/IO


def test_step_budget_stops_before_the_extra_tool_call() -> None:
    tools = ScriptedTools()
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools, steps=1)
    report = run.execute(toy_logic)
    assert (report.status, report.detail) == ("budget_exhausted", "steps")
    assert [c[1] for c in tools.calls] == ["pick"]
    assert report.progress == "partial"  # the pick happened; nothing pretends it did not


def test_tool_returning_after_the_wall_deadline_is_a_timeout() -> None:
    clock = FakeClock()
    tools = ScriptedTools([lambda: (clock.advance(60_000), ToolResult("ok", {}, {}))[1]])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools, clock, wall_ms=50_000)
    report = run.execute(toy_logic)
    assert (report.status, report.detail) == ("timeout", "tool_deadline")
    assert len(tools.calls) == 1


def test_wall_time_expiry_between_calls_is_a_timeout() -> None:
    clock = FakeClock()
    tools = ScriptedTools()
    run, _ = new_run(ScriptedProvider([ok(SELECT, clock, 49_000)]), tools, clock, wall_ms=50_000)

    def logic(r: BoundedRun) -> str:
        r.call_model("planner", {})
        clock.advance(2000)  # harness work between calls
        r.call_tool("pick", {})
        return "completed"

    report = run.execute(logic)
    assert (report.status, report.detail, tools.calls) == ("timeout", "wall_time", [])


@pytest.mark.parametrize("local_work_ms", [1000, 2000])
def test_voluntary_stop_at_or_after_the_wall_deadline_is_a_timeout(local_work_ms: int) -> None:
    clock = FakeClock()

    def place() -> ToolResult:
        clock.advance(49_000)
        return ToolResult("ok", {}, {})

    tools = ScriptedTools([lambda: ToolResult("ok", {}, {}), place])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools, clock, wall_ms=50_000)

    def logic(r: BoundedRun) -> str:
        status = toy_logic(r)  # the last call was accepted at 49_000 ms into the run
        clock.advance(local_work_ms)  # local work reaches (1000) or passes (2000) the deadline
        return status

    report = run.execute(logic)
    assert (report.state, report.status, report.detail) == ("completed", "timeout", "wall_time")
    assert report.progress == "goal_reached"  # the effect is kept, but no success is recorded
    assert report.history[-1]["by"] == "supervisor"
    late = [d["value"] for d in report.diagnostics if d["kind"] == "voluntary_stop_after_deadline"]
    assert late == ["completed"]
    assert run.finish("completed").diagnostic == "lifecycle_violation"
    assert_no_more_calls(run, None, tools)


def test_voluntary_stop_just_before_the_wall_deadline_is_applied() -> None:
    clock = FakeClock()
    run, _ = new_run(clock=clock, wall_ms=50_000)
    run.start()
    clock.advance(49_999)
    assert run.finish("abstained").applied
    assert run.report().status == "abstained"


def test_model_result_exactly_at_its_call_deadline_is_fenced() -> None:
    clock = FakeClock()
    late = {"decision": "select", "skill_id": "late-skill"}
    provider = ScriptedProvider([ok(late, clock, 3000), ok({"decision": "abstain"})])
    run, _ = new_run(provider, ScriptedTools(), clock, model_call_timeout_ms=3000)
    report = run.execute(toy_logic)
    assert report.status == "abstained"  # the selection at the deadline was never applied
    assert [(f["reason"], f["call_id"]) for f in report.fenced] == [("deadline_exceeded", "m1")]


def test_tool_result_exactly_at_the_wall_deadline_is_a_timeout() -> None:
    clock = FakeClock()
    tools = ScriptedTools([lambda: (clock.advance(50_000), ToolResult("ok", {}, {}))[1]])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools, clock, wall_ms=50_000)
    report = run.execute(toy_logic)
    assert (report.status, report.detail) == ("timeout", "tool_deadline")
    assert report.fenced[0]["reason"] == "deadline_exceeded" and len(tools.calls) == 1


def test_submit_exactly_at_the_ticket_deadline_is_fenced() -> None:
    clock = FakeClock()
    run, _ = new_run(clock=clock, wall_ms=50_000)
    run.start()
    ticket = run.ticket("model")
    clock.advance(ticket.deadline_ms - clock())
    assert run.submit(ticket, {"decision": "select"}).reason == "deadline_exceeded"


# ------------------------------------------------------------------- completion provenance


def test_logic_self_reported_goal_cannot_complete_the_run() -> None:
    provider = ScriptedProvider([ok(SELECT)])
    tools = ScriptedTools(progress={})  # the environment observes nothing

    def logic(r: BoundedRun) -> str:
        toy_logic(r)
        r.record_progress("goal_reached")  # self-report by the harness logic
        return "completed"

    run, _ = new_run(provider, tools)
    report = run.execute(logic)
    assert (report.state, report.detail) == ("failed", "run_not_terminated")
    assert (report.progress, report.progress_source) == ("goal_reached", "harness_logic")
    claims = [d for d in report.diagnostics if d["kind"] == "unsupported_completion_claim"]
    assert claims and claims[0]["progress_source"] == "harness_logic"
    assert all(h["on"] != "evaluator_terminal_event" for h in report.history)
    assert report.history[-1]["by"] == "supervisor"


def test_completed_history_records_harness_request_and_environment_progress() -> None:
    tools = ScriptedTools()
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools)
    report = run.execute(toy_logic)
    assert report.status == "completed" and report.progress_source == "environment"
    terminal = report.history[-1]
    assert terminal["edge"] == "evaluator_terminal_event"
    assert (terminal["on"], terminal["by"], terminal["evaluated"]) == (
        "harness_stop_unevaluated",
        "harness_logic",
        False,
    )
    assert (terminal["progress"], terminal["progress_source"]) == ("goal_reached", "environment")
    assert [(o["progress"], o["source"]) for o in report.progress_observations] == [
        ("partial", "environment"),
        ("goal_reached", "environment"),
    ]
    assert all(h["evaluated"] is False for h in report.history)


def test_unknown_progress_source_is_a_caller_error() -> None:
    run, _ = new_run()
    run.start()
    with pytest.raises(ValueError):
        run.record_progress("goal_reached", "model")


def test_total_output_bytes_budget() -> None:
    provider = ScriptedProvider([ok({"blob": "x" * 3000})], repeat_last=True)
    run, _ = new_run(provider, max_total_output_bytes=5000)

    def logic(r: BoundedRun) -> str:
        while True:
            r.call_model("planner", {})

    report = run.execute(logic)
    assert (report.status, report.detail, provider.calls) == (
        "budget_exhausted",
        "total_output_bytes",
        2,
    )


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (lambda: ToolResult("done", {}, {}), "malformed_tool_result"),
        (lambda: ("ok", "not-a-dict", {}), "malformed_tool_result"),
        (lambda: ToolResult("ok", {"blob": "y" * 5000}, {}), "oversized_tool_result"),
        (lambda: (_ for _ in ()).throw(RuntimeError("worker died")), "worker_failure"),
    ],
    ids=["bad_status", "bad_result", "oversized", "crash"],
)
def test_tool_worker_failures_fail_the_run(result: Any, detail: str) -> None:
    tools = ScriptedTools([result])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools)
    report = run.execute(toy_logic)
    assert (report.state, report.detail) == ("failed", detail)
    assert len(tools.calls) == 1


def test_tool_error_status_is_returned_to_the_logic() -> None:
    tools = ScriptedTools([lambda: ToolResult("error", {"why": "blocked"}, {})])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools)

    def logic(r: BoundedRun) -> str:
        r.call_model("planner", {})
        return "blocked" if r.call_tool("pick", {}).status == "error" else "completed"

    assert run.execute(logic).status == "blocked"


# ------------------------------------------------------------------------- tokens and cost


def test_token_limit_fail_closed_on_unknown_usage() -> None:
    provider = ScriptedProvider([ok(SELECT)])
    run, _ = new_run(provider, ScriptedTools(), tokens=1000)
    report = run.execute(toy_logic)
    assert (report.status, report.detail) == ("budget_exhausted", "tokens_unknown")
    assert report.usage["tokens"]["total"] is None and report.usage["tokens"]["unknown_calls"] == 1


def test_token_limit_record_unknown_continues_and_keeps_total_unknown() -> None:
    provider = ScriptedProvider([ok(SELECT)])
    run, _ = new_run(provider, ScriptedTools(), tokens=1000, unknown_usage="record_unknown")
    report = run.execute(toy_logic)
    assert report.status == "completed"
    assert report.usage["tokens"] == {"known": 0, "unknown_calls": 1, "total": None, "limit": 1000}


def test_token_and_cost_overrun_discards_the_crossing_result() -> None:
    usage = {"input_tokens": 60, "output_tokens": 50, "cost_micro_usd": 10}
    provider = ScriptedProvider([ok({**SELECT, "usage": usage})])
    tools = ScriptedTools()
    run, _ = new_run(provider, tools, tokens=100, cost_micro_usd=1000)
    report = run.execute(toy_logic)
    assert (report.status, report.detail, tools.calls) == ("budget_exhausted", "tokens", [])
    assert report.usage["tokens"]["total"] == 110


def test_cost_budget_blocks_the_next_call() -> None:
    usage = {"input_tokens": 1, "output_tokens": 1, "cost_micro_usd": 500}
    provider = ScriptedProvider([ok({"decision": "x", "usage": usage})], repeat_last=True)
    run, _ = new_run(provider, cost_micro_usd=1000)

    def logic(r: BoundedRun) -> str:
        while True:
            r.call_model("planner", {})

    report = run.execute(logic)
    assert (report.status, report.detail, provider.calls) == ("budget_exhausted", "cost", 2)


# ----------------------------------------------------------- attempts and identity fencing


def test_attempt_budget() -> None:
    run, _ = new_run(attempts=2)

    def logic(r: BoundedRun) -> str:
        assert r.attempt_id == "a1"
        assert r.new_attempt() == "a2"
        r.new_attempt()
        return "completed"

    report = run.execute(logic)
    assert (report.status, report.detail, report.attempt_id) == (
        "budget_exhausted",
        "attempts",
        "a2",
    )


def test_results_are_fenced_by_run_attempt_and_call_identity() -> None:
    run, _ = new_run()
    other, _ = new_run(run_id="run-2")
    assert run.start().applied and other.start().applied
    stale = run.ticket("tool")
    assert run.new_attempt() == "a2"
    assert run.submit(stale, {"status": "ok"}).reason == "attempt_mismatch"
    foreign = other.ticket("tool")
    assert run.submit(foreign, {"status": "ok"}).reason == "run_mismatch"
    current = run.ticket("tool")
    assert run.submit(current, {"status": "ok"}).accepted
    assert run.submit(current, {"status": "ok"}).reason == "already_settled"
    assert run.submit("forged", {}).reason == "malformed_ticket"  # type: ignore[arg-type]
    report = run.report()
    assert [f["reason"] for f in report.fenced] == [
        "attempt_mismatch",
        "run_mismatch",
        "already_settled",
        "malformed_ticket",
    ]
    assert report.state == "running"


def test_late_result_after_cancellation_is_fenced_and_effects_are_retained() -> None:
    run, _ = new_run()
    run.start()
    in_flight = run.ticket("tool")
    run.record_progress("partial", "environment")  # the environment observed the pick happen
    assert run.cancel().applied
    settlement = run.submit(in_flight, {"status": "ok", "result": {"picked": True}})
    assert (settlement.accepted, settlement.reason) == (False, "not_running")
    assert run.complete_cancellation().applied
    late = run.submit(in_flight, {"status": "ok"})
    assert late.reason == "already_settled"
    report = run.report()
    assert (report.state, report.status, report.progress) == ("cancelled", "cancelled", "partial")
    assert report.fenced[0]["payload_sha256"] == sha256_of(
        {"status": "ok", "result": {"picked": True}}
    )


def test_abandoned_call_from_closed_attempt_is_fenced_after_terminal() -> None:
    run, _ = new_run()
    run.start()
    t = run.ticket("model")
    assert run.finish("abstained").applied
    assert run.submit(t, {"decision": "select"}).reason == "call_abandoned"


# ------------------------------------------------------------------ cancellation races


def test_cancel_during_model_call_fences_result_and_stops() -> None:
    holder: dict[str, BoundedRun] = {}

    def cancel_then_answer() -> dict[str, Any]:
        holder["run"].cancel("goal_withdrawn")
        return SELECT

    provider = ScriptedProvider([cancel_then_answer, ok(SELECT)])
    tools = ScriptedTools()
    run, _ = new_run(provider, tools)
    holder["run"] = run
    report = run.execute(toy_logic)
    assert (report.state, report.status, report.detail) == (
        "cancelled",
        "cancelled",
        "safe_stop_reported",
    )
    assert provider.calls == 1 and tools.calls == []
    assert report.fenced[0]["reason"] == "not_running"
    assert_no_more_calls(run, provider, tools)


def test_cancel_during_tool_call_keeps_partial_progress() -> None:
    holder: dict[str, BoundedRun] = {}

    def pick_then_cancelled() -> ToolResult:
        holder["run"].record_progress("partial", "environment")
        holder["run"].cancel()
        return ToolResult("ok", {"picked": True}, {})

    tools = ScriptedTools([pick_then_cancelled])
    run, _ = new_run(ScriptedProvider([ok(SELECT)]), tools)
    holder["run"] = run
    report = run.execute(toy_logic)
    assert (report.state, report.progress) == ("cancelled", "partial")
    assert [c[1] for c in tools.calls] == ["pick"]  # place never ran


def test_cancel_racing_completion_first_request_wins_both_orders() -> None:
    run, _ = new_run()
    run.start()
    run.record_progress("goal_reached", "environment")
    assert run.finish("completed").applied
    assert run.cancel().diagnostic == "lifecycle_violation"
    assert run.report().status == "completed"

    run2, _ = new_run()
    run2.start()
    run2.record_progress("goal_reached", "environment")
    assert run2.cancel().applied
    assert run2.finish("completed").diagnostic == "lifecycle_violation"
    assert run2.complete_cancellation().applied
    report = run2.report()
    assert (report.status, report.progress) == ("cancelled", "goal_reached")


def test_concurrent_cancel_and_completion_always_end_in_one_terminal_state() -> None:
    for i in range(200):
        provider = ScriptedProvider([ok(SELECT)], repeat_last=True)
        tools = ScriptedTools()
        run, _ = new_run(provider, tools, run_id=f"race-{i}")
        barrier = threading.Barrier(2)

        def canceller(r: BoundedRun = run, b: threading.Barrier = barrier) -> None:
            b.wait()
            r.cancel()
            r.cancel()

        def logic(r: BoundedRun, b: threading.Barrier = barrier) -> str:
            b.wait()
            toy_logic(r)
            return "completed"

        thread = threading.Thread(target=canceller)
        thread.start()
        report = run.execute(logic)
        thread.join(5)
        assert not thread.is_alive()
        assert report.state in ("completed", "cancelled")
        assert len([h for h in report.history if h["to"] in ("completed", "cancelled")]) == 1
        assert provider.calls <= 1 and len(tools.calls) <= 2
        assert_no_more_calls(run, provider, tools)


def test_cancel_before_start_never_runs_logic() -> None:
    run, _ = new_run()
    assert run.cancel().applied
    called = []
    report = run.execute(lambda r: called.append(1) or "completed")
    assert (report.state, report.progress, called) == ("cancelled", "none", [])


def test_rejected_run_does_nothing() -> None:
    provider = ScriptedProvider([ok(SELECT)])
    run, _ = new_run(provider)
    assert run.reject("bad_input").applied
    assert run.report().state == "rejected"
    assert run.execute(toy_logic).state == "rejected"
    assert provider.calls == 0


# --------------------------------------------------------- logic errors and resources


def test_logic_errors_and_invalid_returns_fail_the_run() -> None:
    def boom(r: BoundedRun) -> str:
        raise KeyError("bug")

    assert new_run()[0].execute(boom).detail == "harness_logic_error"
    assert new_run()[0].execute(lambda r: "succeeded").detail == "invalid_logic_status"
    # Claiming completion without observed goal progress is refused, then the run fails.
    report = new_run()[0].execute(lambda r: "completed")
    assert (report.state, report.detail) == ("failed", "run_not_terminated")
    assert any(d["kind"] == "lifecycle_violation" for d in report.diagnostics)


def test_resources_are_released_once_in_reverse_order_even_when_one_fails() -> None:
    released: list[str] = []
    run, _ = new_run(ScriptedProvider([fail(WorkerFailure("crash"))]))
    run.add_resource("sim", lambda: released.append("sim"))

    def broken() -> None:
        raise OSError("already gone")

    run.add_resource("broken", broken)
    run.add_resource("worker", lambda: released.append("worker"))
    report = run.execute(toy_logic)
    run.fail("again")
    run.cancel()
    assert released == ["worker", "sim"]
    assert run.report().released == ["worker", "sim"]
    assert any(d["kind"] == "release_failed" for d in report.diagnostics)
    run.add_resource("late", lambda: released.append("late"))
    assert released == ["worker", "sim", "late"]


def test_resources_are_released_on_cancellation() -> None:
    released: list[str] = []
    run, _ = new_run()
    run.start()
    run.add_resource("worker", lambda: released.append("worker"))
    run.cancel()
    assert released == []  # still cancelling: in-flight work may need the resource
    run.complete_cancellation()
    assert released == ["worker"]


def test_result_fenced_is_raised_when_attempt_changes_during_a_call() -> None:
    holder: dict[str, BoundedRun] = {}

    def answer_after_new_attempt() -> dict[str, Any]:
        holder["run"].new_attempt()
        return SELECT

    run, _ = new_run(ScriptedProvider([answer_after_new_attempt]))
    holder["run"] = run
    run.start()
    with pytest.raises(ResultFenced) as exc:
        run.call_model("planner", {})
    assert exc.value.reason == "attempt_mismatch" and run.state == "running"


def test_interrupt_still_terminates_and_releases_before_propagating() -> None:
    released: list[str] = []
    run, _ = new_run()
    run.add_resource("worker", lambda: released.append("worker"))

    def interrupted(r: BoundedRun) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run.execute(interrupted)
    assert (run.state, run.report().detail, released) == ("failed", "interrupted", ["worker"])
