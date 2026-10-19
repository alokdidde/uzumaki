import socket
from typing import Any

import pytest

import toy_harness
from toy_harness import MODELS, SKILLS, record, run
from uzumaki.evidence import DecisionSource, EventRecorder, EvidenceError, ToolExecutor
from uzumaki.providers import RecordedProvider
from uzumaki.replay import (
    CLAIM_REQUIREMENTS,
    NotReplayable,
    RecordedDecisions,
    ReplayDivergence,
    ReplayMode,
    UnsupportedReplayMode,
    assess_claims,
    replay,
    semantic_outcome,
)


@pytest.fixture
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider accessed during recorded-decision replay")

    monkeypatch.setattr(RecordedProvider, "request", forbidden)
    monkeypatch.setattr(RecordedProvider, "from_path", forbidden)
    monkeypatch.setattr(toy_harness.FakeEnv, "execute", forbidden)


def do_replay(bundle: Any, runner: Any = run, mode: Any = "recorded_decision", **kw: Any) -> Any:
    versions: dict[str, Any] = {"skill_versions": SKILLS, "model_versions": MODELS}
    versions.update(kw)
    return replay(bundle, mode, runner, **versions)


@pytest.mark.parametrize("observation_id", ["obs-1", "obs-2"])
def test_recorded_decision_replay_matches_without_provider_or_environment(
    observation_id: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    bundle = record(observation_id)
    request.getfixturevalue("no_provider")
    report = do_replay(bundle)
    assert report.mode is ReplayMode.RECORDED_DECISION
    assert report.matches and report.differences == ()
    assert semantic_outcome(report.replayed) == semantic_outcome(bundle)
    origin = report.replayed["manifest"]["origin"]
    assert origin == {"mode": "recorded_decision", "source_head_sha256": report.source_head_sha256}
    # Timings differ, so the logs are not byte-identical; semantics are.
    assert report.replay_head_sha256 != report.source_head_sha256
    with pytest.raises(AssertionError, match="offline"):
        socket.create_connection(("example.com", 443))


def test_replay_reports_semantic_difference_in_derived_outcome() -> None:
    bundle = record()

    def buggy(rec: EventRecorder, d: DecisionSource, t: ToolExecutor) -> dict[str, Any]:
        obs = rec.call_tool(t, "call-1", "observe", {})
        rec.observation(obs.result["observation_id"], "fake_env", 5, obs.result)
        rec.decide(d, "dec-1", "planner", {"role": "planner", "observation_id": "obs-1"})
        rec.outcome("abstained", "none")  # ignores the selection and skips the tool calls
        return rec.finish("completed")

    report = do_replay(bundle, buggy)
    assert not report.matches
    assert report.differences == ("tool_calls", "outcome", "unconsumed_tool_calls")


def test_replay_raises_when_logic_requests_unrecorded_input() -> None:
    bundle = record()

    def diverging(rec: EventRecorder, d: DecisionSource, t: ToolExecutor) -> dict[str, Any]:
        rec.call_tool(t, "call-1", "observe", {"zoom": 2})
        raise AssertionError("unreachable")

    with pytest.raises(ReplayDivergence, match="tool call 0"):
        do_replay(bundle, diverging)
    decisions = RecordedDecisions(bundle)
    with pytest.raises(ReplayDivergence, match="decision 0"):
        decisions.decide("dec-1", "planner", {"role": "planner", "observation_id": "obs-9"})


@pytest.mark.parametrize(
    "mode", [ReplayMode.FRESH_MODEL, ReplayMode.ENVIRONMENT_RESIMULATION, "fresh_model"]
)
def test_unimplemented_modes_are_rejected_and_never_substituted(mode: Any) -> None:
    bundle = record()
    called = []

    def runner(*args: Any) -> dict[str, Any]:
        called.append(1)
        return run(*args)

    with pytest.raises(UnsupportedReplayMode):
        do_replay(bundle, runner, mode=mode)
    assert called == []


def test_claims_declare_requirements_and_availability() -> None:
    claims = assess_claims(record())
    assert claims[ReplayMode.RECORDED_DECISION].available
    assert not claims[ReplayMode.FRESH_MODEL].available
    assert claims[ReplayMode.FRESH_MODEL].missing == ("mode_not_implemented",)
    assert not claims[ReplayMode.ENVIRONMENT_RESIMULATION].available
    assert set(CLAIM_REQUIREMENTS) == set(ReplayMode)
    assert all(CLAIM_REQUIREMENTS[m] for m in ReplayMode)


def test_redacted_replay_input_makes_replay_unavailable() -> None:
    bundle = record("obs-3")
    claim = assess_claims(bundle)[ReplayMode.RECORDED_DECISION]
    assert not claim.available
    assert claim.missing == ("required_input_redacted:seq=4:output.api_key",)
    with pytest.raises(NotReplayable):
        do_replay(bundle)


def test_redacted_non_required_field_still_replays() -> None:
    bundle = record(diagnostics={"token": "sk-fixture-not-real"})
    report = do_replay(bundle)
    assert report.matches
    assert "sk-fixture-not-real" not in str(report.replayed)


@pytest.mark.parametrize(
    "kw",
    [
        {"skill_versions": {"pick-cube": "1.0.1"}},
        {"model_versions": {"planner": "fixture-planner-2"}},
    ],
)
def test_replay_rejects_version_mismatch(kw: dict[str, Any]) -> None:
    with pytest.raises(EvidenceError) as exc:
        do_replay(record(), **kw)
    assert exc.value.reason == "version_mismatch"


def test_replay_rejects_invalid_evidence_before_running() -> None:
    bundle = record()
    bundle["events"].pop()
    with pytest.raises(EvidenceError) as exc:
        do_replay(bundle)
    assert exc.value.reason == "truncated"


def test_runner_must_return_this_replay() -> None:
    original = record()

    def substitute(*args: Any) -> dict[str, Any]:
        return original  # a stale original log cannot pose as a replay

    with pytest.raises(ReplayDivergence, match="not this replay"):
        do_replay(original, substitute)
