"""Replay modes for recorded evidence.

Three modes are distinct and never substituted for one another:

- ``recorded_decision``: re-drive harness logic with the recorded decisions and recorded tool
  results. No provider, model or environment is used. Supported.
- ``fresh_model_reevaluation``: ask a live model again. Unsupported; always rejected.
- ``environment_resimulation``: re-run the environment or simulator. Unsupported; always rejected.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from uzumaki.evidence import (
    DecisionSource,
    EventRecorder,
    EvidenceError,
    Limits,
    ToolExecutor,
    ToolResult,
    sha256_of,
    validate_bundle,
)


class ReplayMode(StrEnum):
    RECORDED_DECISION = "recorded_decision"
    FRESH_MODEL = "fresh_model_reevaluation"
    ENVIRONMENT_RESIMULATION = "environment_resimulation"


SUPPORTED_MODES = frozenset({ReplayMode.RECORDED_DECISION})

# Artifacts that must be present and intact before a claim of each mode can be made.
CLAIM_REQUIREMENTS: dict[ReplayMode, tuple[str, ...]] = {
    ReplayMode.RECORDED_DECISION: (
        "manifest (run/task/candidate IDs, contract, skill and model versions, limits)",
        "complete checksum-chained event log ending in run_finished",
        "unredacted decision.output for every decision",
        "unredacted tool_call.arguments and tool_result.result for every tool call",
        "exactly one unredacted outcome",
    ),
    ReplayMode.FRESH_MODEL: (
        "versioned prompt templates and full model inputs (not only digests)",
        "explicitly authorized live provider access and inference budget",
        "not implemented",
    ),
    ReplayMode.ENVIRONMENT_RESIMULATION: (
        "pinned simulator, model and asset hashes, seeds and reset procedure",
        "an environment adapter",
        "not implemented",
    ),
}


class UnsupportedReplayMode(ValueError):
    """The requested mode is unknown or not implemented. No other mode is tried instead."""


class NotReplayable(ValueError):
    """The evidence is valid but lacks inputs required for the requested replay claim."""


class ReplayDivergence(ValueError):
    """Replayed logic requested something the recording cannot answer."""


@dataclass(frozen=True)
class ClaimAssessment:
    mode: ReplayMode
    available: bool
    required: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class SemanticOutcome:
    """Timing-independent content of a run used to compare an original and a replay."""

    observations: tuple[tuple[str, str, str], ...]
    decisions: tuple[tuple[str, str, str, str, str], ...]
    tool_calls: tuple[tuple[str, str, str | None, str, str, str], ...]
    outcome: tuple[str, str]
    status: str


@dataclass(frozen=True)
class ReplayReport:
    mode: ReplayMode
    source_head_sha256: str
    replay_head_sha256: str
    matches: bool
    differences: tuple[str, ...]
    replayed: dict[str, Any]


Runner = Callable[[EventRecorder, DecisionSource, ToolExecutor], dict[str, Any]]


def _mode(mode: ReplayMode | str) -> ReplayMode:
    try:
        return ReplayMode(mode)
    except ValueError:
        raise UnsupportedReplayMode("unknown replay mode") from None


def assess_claims(bundle: Any) -> dict[ReplayMode, ClaimAssessment]:
    """Validate ``bundle`` and state which replay claims it can support."""
    validate_bundle(bundle)
    manifest = bundle["manifest"]
    result: dict[ReplayMode, ClaimAssessment] = {}
    for mode in ReplayMode:
        if mode not in SUPPORTED_MODES:
            missing: tuple[str, ...] = ("mode_not_implemented",)
        else:
            missing = tuple(manifest["non_replayable"])
        result[mode] = ClaimAssessment(mode, not missing, CLAIM_REQUIREMENTS[mode], missing)
    return result


def semantic_outcome(bundle: Any) -> SemanticOutcome:
    validate_bundle(bundle)
    observations = []
    decisions = []
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    outcome = ("", "")
    status = ""
    for event in bundle["events"]:
        d = event["data"]
        kind = event["kind"]
        if kind == "observation":
            observations.append((d["observation_id"], d["source"], d["content_sha256"]))
        elif kind == "decision":
            decisions.append(
                (d["decision_id"], d["role"], d["model"], d["input_sha256"], sha256_of(d["output"]))
            )
        elif kind == "tool_call":
            calls[d["call_id"]] = d
            order.append(d["call_id"])
        elif kind == "tool_result":
            calls[d["call_id"]] = {**calls[d["call_id"]], "_result": d}
        elif kind == "outcome":
            outcome = (d["primary"], d["progress"])
        elif kind == "run_finished":
            status = d["status"]
    tool_calls = tuple(
        (
            cid,
            calls[cid]["tool"],
            calls[cid]["skill_id"],
            sha256_of(calls[cid]["arguments"]),
            calls[cid]["_result"]["status"],
            sha256_of(calls[cid]["_result"]["result"]),
        )
        for cid in order
    )
    return SemanticOutcome(tuple(observations), tuple(decisions), tool_calls, outcome, status)


class RecordedDecisions:
    """Serves recorded decisions in order. Never contacts a provider or model."""

    def __init__(self, bundle: Mapping[str, Any]) -> None:
        self._queue = [e["data"] for e in bundle["events"] if e["kind"] == "decision"]
        self._next = 0

    @property
    def remaining(self) -> int:
        return len(self._queue) - self._next

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        index = self._next
        if index >= len(self._queue):
            raise ReplayDivergence(f"decision {index} was not recorded")
        recorded = self._queue[index]
        if (
            recorded["decision_id"] != decision_id
            or recorded["role"] != role
            or recorded["input_sha256"] != sha256_of(request)
        ):
            raise ReplayDivergence(f"decision {index} request differs from the recording")
        self._next += 1
        return copy.deepcopy(recorded["output"])


class RecordedTools:
    """Serves recorded tool results for identical calls, in order. Never runs an environment."""

    def __init__(self, bundle: Mapping[str, Any]) -> None:
        results = {
            e["data"]["call_id"]: e["data"] for e in bundle["events"] if e["kind"] == "tool_result"
        }
        self._queue = [
            (e["data"], results[e["data"]["call_id"]])
            for e in bundle["events"]
            if e["kind"] == "tool_call"
        ]
        self._next = 0

    @property
    def remaining(self) -> int:
        return len(self._queue) - self._next

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        index = self._next
        if index >= len(self._queue):
            raise ReplayDivergence(f"tool call {index} was not recorded")
        call, result = self._queue[index]
        if (call["call_id"], call["tool"], call["skill_id"]) != (call_id, tool, skill_id) or (
            sha256_of(call["arguments"]) != sha256_of(arguments)
        ):
            raise ReplayDivergence(f"tool call {index} differs from the recording")
        self._next += 1
        return ToolResult(
            result["status"], copy.deepcopy(result["result"]), copy.deepcopy(result["diagnostics"])
        )


def _counter_clock() -> Callable[[], int]:
    state = {"t": 0}

    def tick() -> int:
        state["t"] += 1
        return state["t"]

    return tick


def replay(
    bundle: Any,
    mode: ReplayMode | str,
    runner: Runner,
    *,
    skill_versions: Mapping[str, str],
    model_versions: Mapping[str, str],
) -> ReplayReport:
    """Replay ``bundle`` in exactly ``mode`` and compare semantic outcomes.

    ``runner`` is the deterministic harness logic under test. It receives a fresh recorder plus
    recorded decision and tool sources and must return ``recorder.finish(...)``. The current
    ``skill_versions``/``model_versions`` must equal the recorded ones.
    """
    requested = _mode(mode)
    if requested not in SUPPORTED_MODES:
        raise UnsupportedReplayMode(f"{requested.value} replay is not implemented")
    validate_bundle(bundle)
    manifest = bundle["manifest"]
    if not manifest["replayable"]:
        raise NotReplayable("required replay inputs were redacted; run is non-replayable")
    if (
        dict(skill_versions) != manifest["skill_versions"]
        or dict(model_versions) != manifest["model_versions"]
    ):
        raise EvidenceError("version_mismatch", "replay versions differ from the recording")
    recorder = EventRecorder(
        run_id=manifest["run_id"],
        task_id=manifest["task_id"],
        candidate_id=manifest["candidate_id"],
        skill_versions=skill_versions,
        model_versions=model_versions,
        limits=Limits(**manifest["limits"]),
        clock=_counter_clock(),
        origin={"mode": requested.value, "source_head_sha256": manifest["head_sha256"]},
    )
    decisions = RecordedDecisions(bundle)
    tools = RecordedTools(bundle)
    replayed = runner(recorder, decisions, tools)
    validate_bundle(replayed)
    if replayed["manifest"]["origin"] != {
        "mode": requested.value,
        "source_head_sha256": manifest["head_sha256"],
    }:
        raise ReplayDivergence("runner returned evidence that is not this replay")
    original = semantic_outcome(bundle)
    again = semantic_outcome(replayed)
    differences = [
        f.name
        for f in fields(SemanticOutcome)
        if getattr(original, f.name) != getattr(again, f.name)
    ]
    if decisions.remaining:
        differences.append("unconsumed_decisions")
    if tools.remaining:
        differences.append("unconsumed_tool_calls")
    return ReplayReport(
        mode=requested,
        source_head_sha256=manifest["head_sha256"],
        replay_head_sha256=replayed["manifest"]["head_sha256"],
        matches=not differences,
        differences=tuple(differences),
        replayed=replayed,
    )
