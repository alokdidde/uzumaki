import copy
import json
from pathlib import Path
from typing import Any

import pytest

from toy_harness import FakeEnv, ProviderDecisions, new_recorder, record, rehash
from uzumaki.evidence import (
    LOG_VERSION,
    EvidenceError,
    Limits,
    load_bundle,
    validate_bundle,
    write_bundle,
)


def reason(bundle: Any) -> str:
    with pytest.raises(EvidenceError) as exc:
        validate_bundle(bundle)
    return exc.value.reason


def test_records_ids_versions_observations_decisions_tools_outcome_and_timings() -> None:
    bundle = record()
    validate_bundle(bundle)
    m = bundle["manifest"]
    assert (m["run_id"], m["task_id"], m["candidate_id"]) == ("run-1", "pick-place-v1", "cand-1")
    assert m["contract_version"] == "1.0" and bundle["log_version"] == LOG_VERSION
    assert m["skill_versions"] == {"pick-cube": "1.0.0"}
    assert m["model_versions"] == {"planner": "fixture-planner-1"}
    assert m["replayable"] is True and m["non_replayable"] == []
    kinds = [e["kind"] for e in bundle["events"]]
    assert kinds == [
        "run_started",
        "tool_call",
        "tool_result",
        "observation",
        "decision",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "outcome",
        "run_finished",
    ]
    by_kind = {e["kind"]: e["data"] for e in bundle["events"]}
    assert by_kind["observation"]["observation_id"] == "obs-1"
    assert "object" not in by_kind["observation"]  # identity and digest only, no raw content
    assert by_kind["decision"]["model"] == "fixture-planner-1"
    assert by_kind["decision"]["output"] == {"decision": "select", "skill_id": "pick-cube"}
    assert by_kind["decision"]["duration_ms"] == 3  # one clock step with the fake clock
    assert by_kind["outcome"] == {"primary": "completed", "progress": "goal_reached"}
    pick = [e["data"] for e in bundle["events"] if e["kind"] == "tool_call"][1]
    assert pick["skill_id"] == "pick-cube"
    times = [e["monotonic_ms"] for e in bundle["events"]]
    assert times == sorted(times)
    assert all(e["data"]["duration_ms"] > 0 for e in bundle["events"] if e["kind"] == "tool_result")


def test_recording_is_deterministic_with_fixed_clock() -> None:
    assert record()["manifest"]["head_sha256"] == record()["manifest"]["head_sha256"]


def test_round_trip_and_no_overwrite(tmp_path: Path) -> None:
    bundle = record()
    path = tmp_path / "run.json"
    write_bundle(bundle, path)
    assert load_bundle(path) == bundle
    with pytest.raises(EvidenceError, match="overwrite"):
        write_bundle(bundle, path)


def test_rejects_truncated_evidence(tmp_path: Path) -> None:
    bundle = record()
    tail = copy.deepcopy(bundle)
    tail["events"].pop()
    assert reason(tail) == "truncated"
    middle = copy.deepcopy(bundle)
    del middle["events"][4]
    assert reason(middle) == "truncated"
    path = tmp_path / "cut.json"
    text = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    for cut in (len(text) // 2, len(text) // 3, len(text) - 1):
        path.write_text(text[:cut], encoding="ascii")
        with pytest.raises(EvidenceError) as exc:
            load_bundle(path)
        assert exc.value.reason == "truncated"


def test_rejects_reordered_evidence() -> None:
    bundle = record()
    bundle["events"][5], bundle["events"][6] = bundle["events"][6], bundle["events"][5]
    assert reason(bundle) == "reordered"


def test_rejects_reordering_even_with_a_consistent_checksum_chain() -> None:
    bundle = record()
    bundle["events"][5], bundle["events"][6] = bundle["events"][6], bundle["events"][5]
    assert reason(rehash(bundle)) == "reordered"  # tool result before its call
    later = record()
    later["events"][3]["monotonic_ms"] = 10**6
    assert reason(rehash(later)) == "reordered"  # time goes backwards afterwards


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["events"][4]["data"]["output"].update(skill_id="other"),
        lambda b: b["events"][2]["data"]["result"].update(done=False),
        lambda b: b["manifest"].update(head_sha256="0" * 64),
        lambda b: b["manifest"].update(run_id="run-2"),
        lambda b: b["manifest"].update(candidate_id=None),
        lambda b: b["events"][3].update(sha256="f" * 64),
        lambda b: b["events"].append(copy.deepcopy(b["events"][-1])),
    ],
)
def test_rejects_corrupt_evidence(mutate: Any) -> None:
    bundle = record()
    mutate(bundle)
    assert reason(bundle) == "corrupt"


def test_corrupt_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(b'{"log_version":}' + b" " * 10)
    with pytest.raises(EvidenceError) as exc:
        load_bundle(path)
    assert exc.value.reason == "corrupt"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(EvidenceError) as exc:
        load_bundle(path)
    assert exc.value.reason == "corrupt"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(log_version="uzumaki.events/2.0"),
        lambda b: b["manifest"].update(contract_version="1.1"),
    ],
)
def test_rejects_version_mismatch(mutate: Any) -> None:
    bundle = record()
    mutate(bundle)
    assert reason(bundle) == "version_mismatch"


def test_rejects_decision_model_that_differs_from_manifest() -> None:
    bundle = record()
    bundle["events"][4]["data"]["model"] = "other-model"
    assert reason(rehash(bundle)) == "version_mismatch"


def test_rejects_incomplete_evidence() -> None:
    bundle = record()
    del bundle["events"][9]  # the outcome
    assert reason(rehash(bundle)) == "incomplete"
    unanswered = record()
    del unanswered["events"][8]  # last tool_result
    assert reason(rehash(unanswered)) == "incomplete"


def test_rejects_malformed_evidence() -> None:
    assert reason([]) == "malformed"
    bundle = record()
    bundle["events"][3]["data"]["observation_id"] = "Not An ID"
    assert reason(bundle) == "malformed"
    extra = record()
    extra["manifest"]["notes"] = "x"
    assert reason(extra) == "malformed"


def test_sensitive_keys_are_redacted_and_declared() -> None:
    bundle = record(diagnostics={"token": "sk-fixture-not-real", "level": 1})
    text = json.dumps(bundle)
    assert "sk-fixture-not-real" not in text
    result = bundle["events"][2]
    assert result["redacted_paths"] == ["diagnostics.token"]
    assert result["data"]["diagnostics"] == {"token": {"$redacted": "sensitive_key"}, "level": 1}
    # Diagnostics are not replay inputs, so the run stays replayable.
    assert bundle["manifest"]["replayable"] is True


def test_redacting_a_required_replay_input_marks_run_non_replayable() -> None:
    bundle = record(observation_id="obs-3")  # decision output carries an api_key
    assert "sk-fixture-not-real" not in json.dumps(bundle)
    assert bundle["manifest"]["replayable"] is False
    assert bundle["manifest"]["non_replayable"] == ["required_input_redacted:seq=4:output.api_key"]


def test_rejects_false_replayability_claim_after_redaction() -> None:
    bundle = record(observation_id="obs-3")
    bundle["manifest"].update(replayable=True, non_replayable=[])
    assert reason(rehash(bundle)) == "redaction_inconsistent"


def test_rejects_undeclared_redaction() -> None:
    bundle = record()
    bundle["events"][4]["data"]["output"]["skill_id"] = {"$redacted": "sensitive_key"}
    assert reason(rehash(bundle)) == "redaction_inconsistent"


def test_event_count_is_bounded() -> None:
    # One slot stays reserved for run_finished, so a fourth event is refused before it is recorded.
    rec = new_recorder(limits=Limits(max_events=4))
    rec.tool_call("call-1", "observe", {})
    rec.tool_result("call-1", "ok", {})
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-2", "observe", {})
    assert exc.value.reason == "oversized"
    with pytest.raises(EvidenceError) as exc:
        rec.outcome("failed", "none")
    assert exc.value.reason == "recorder_state"  # no partial bundle after a limit hit


def test_event_and_bundle_bytes_are_bounded(tmp_path: Path) -> None:
    rec = new_recorder(limits=Limits(max_event_bytes=1024))
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-1", "observe", {"blob": "x" * 2000})
    assert exc.value.reason == "oversized"
    big = new_recorder(limits=Limits(max_bundle_bytes=2048))
    with pytest.raises(EvidenceError) as exc:
        for i in range(50):
            big.tool_call(f"call-{i}", "observe", {})
    assert exc.value.reason == "oversized"
    path = tmp_path / "big.json"
    path.write_text(" " * 5000, encoding="ascii")
    with pytest.raises(EvidenceError) as exc:
        load_bundle(path, Limits(max_bundle_bytes=4096))
    assert exc.value.reason == "oversized"


def test_recorder_rejects_limits_the_validator_would_reject() -> None:
    for limits in (
        Limits(max_events=5000),
        Limits(max_event_bytes=Limits().max_event_bytes + 1),
        Limits(max_bundle_bytes=Limits().max_bundle_bytes + 1),
    ):
        with pytest.raises(EvidenceError) as exc:
            new_recorder(limits=limits)
        assert exc.value.reason == "oversized"
    with pytest.raises(EvidenceError) as exc:
        new_recorder(limits=Limits(max_events=0))
    assert exc.value.reason == "malformed"


def test_run_filling_the_default_event_limit_finishes_and_loads(tmp_path: Path) -> None:
    rec = new_recorder()
    for i in range(2046):
        rec.tool_call(f"call-{i}", "observe", {})
        rec.tool_result(f"call-{i}", "ok", {})
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-x", "observe", {})
        rec.tool_result("call-x", "ok", {})
        rec.outcome("completed", "goal_reached")  # would leave no slot for run_finished
    assert exc.value.reason == "oversized"
    rec = new_recorder()
    for i in range(2046):
        rec.tool_call(f"call-{i}", "observe", {})
        rec.tool_result(f"call-{i}", "ok", {})
    rec.outcome("completed", "goal_reached")
    bundle = rec.finish("completed")
    assert len(bundle["events"]) == 4095
    path = tmp_path / "full.json"
    write_bundle(bundle, path)
    assert load_bundle(path) == bundle


def _record_near_byte_limit(limit: int) -> dict[str, Any] | None:
    """Record a run with required-input redactions; None if a limit stops it at record time."""
    rec = new_recorder(limits=Limits(max_bundle_bytes=limit))
    try:
        for i in range(3):
            rec.tool_call(f"call-{i}", "configure", {"api_key": "k", "n": i})
            rec.tool_result(f"call-{i}", "ok", {"token": "t"})
        rec.outcome("completed", "goal_reached")
    except EvidenceError as exc:
        assert exc.reason == "oversized"
        with pytest.raises(EvidenceError) as state:
            rec.finish("completed")
        assert state.value.reason == "recorder_state"
        return None
    bundle: dict[str, Any] = rec.finish("completed")  # must never fail once the outcome is in
    return bundle


def test_byte_limit_is_enforced_exactly_at_record_time() -> None:
    results = {limit: _record_near_byte_limit(limit) for limit in range(3000, 4500)}
    accepted = [limit for limit, bundle in results.items() if bundle is not None]
    assert accepted and accepted[0] > 3000  # the range spans the boundary
    assert accepted == list(range(accepted[0], 4500))  # monotone: no failures past the boundary
    for limit in accepted:
        bundle = results[limit]
        assert bundle is not None
        assert len(bundle["manifest"]["non_replayable"]) == 6
        size = len(json.dumps(bundle, sort_keys=True, separators=(",", ":")))
        assert size <= limit
        validate_bundle(bundle)
    # The only slack is the reserved worst-case run_finished time and status.
    first = results[accepted[0]]
    assert first is not None
    assert accepted[0] - len(json.dumps(first, sort_keys=True, separators=(",", ":"))) < 32


def test_validator_enforces_declared_limits() -> None:
    bundle = record()
    with pytest.raises(EvidenceError) as exc:
        validate_bundle(bundle, Limits(max_events=5))
    assert exc.value.reason == "oversized"


def test_recorder_rejects_out_of_order_and_unversioned_operations() -> None:
    rec = new_recorder()
    with pytest.raises(EvidenceError) as exc:
        rec.tool_result("call-9", "ok", {})
    assert exc.value.reason == "reordered"
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-1", "pick", {}, skill_id="unknown-skill")
    assert exc.value.reason == "version_mismatch"
    with pytest.raises(EvidenceError) as exc:
        rec.decide(ProviderDecisions(None), "dec-1", "generator", {})  # type: ignore[arg-type]
    assert exc.value.reason == "version_mismatch"
    with pytest.raises(EvidenceError) as exc:
        rec.finish("completed")
    assert exc.value.reason == "recorder_state"  # no outcome yet
    rec.tool_call("call-1", "observe", {})
    with pytest.raises(EvidenceError) as exc:
        rec.outcome("completed", "goal_reached")
    assert exc.value.reason == "incomplete"


def test_recorder_rejects_backwards_clock() -> None:
    times = iter([10, 5])
    rec = new_recorder(clock=lambda: next(times))
    with pytest.raises(EvidenceError) as exc:
        rec.call_tool(FakeEnv(), "call-1", "observe", {})
    assert exc.value.reason == "reordered"


def test_errors_never_echo_content() -> None:
    rec = new_recorder()
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-1", "observe", {"x": object(), "secret": "sk-fixture-not-real"})
    assert "sk-fixture" not in str(exc.value)


def test_deeply_nested_event_data_has_safe_error() -> None:
    nested: Any = "private-sentinel"
    for _ in range(5000):
        nested = [nested]
    rec = new_recorder()
    with pytest.raises(EvidenceError) as exc:
        rec.tool_call("call-1", "observe", {"deep": nested})
    assert exc.value.reason == "malformed"
    assert "private-sentinel" not in str(exc.value)
