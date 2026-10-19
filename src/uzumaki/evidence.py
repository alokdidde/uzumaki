"""Versioned, bounded, hash-chained execution event log (evidence bundle).

A bundle is one JSON object ``{"log_version", "manifest", "events"}``. The manifest binds the run,
task and candidate IDs, the contract version, skill/model versions and storage limits. Events form a
SHA-256 chain that starts from the manifest identity, so truncation, reordering, edits and identity
changes are detected. Checksums detect accidental corruption only; they are not signatures.

Sensitive keys are redacted at record time. Redaction is never silent: every redacted path is
listed on its event, and a redaction inside an input that replay needs marks the run non-replayable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

LOG_VERSION = "uzumaki.events/1.0"
CONTRACT_VERSION = "1.0"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REDACTION_MARKER = "$redacted"
DEFAULT_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)

EVENT_KINDS = (
    "run_started",
    "observation",
    "decision",
    "tool_call",
    "tool_result",
    "outcome",
    "run_finished",
)
TERMINAL_STATUSES = ("completed", "cancelled", "failed", "rejected")
TOOL_RESULT_STATUSES = ("ok", "error", "rejected")
ROLES = ("planner", "generator")

# Event data fields that recorded-decision replay feeds back into the harness or compares.
# A redaction at or below any of these paths makes the run non-replayable.
REPLAY_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "decision": ("output",),
    "tool_call": ("arguments",),
    "tool_result": ("result",),
    "outcome": ("primary", "progress"),
}

_EVENT_FIELDS: dict[str, dict[str, str]] = {
    "run_started": {},
    "observation": {
        "observation_id": "id",
        "source": "text",
        "captured_at_ms": "ms",
        "content_sha256": "sha",
    },
    "decision": {
        "decision_id": "id",
        "role": "role",
        "model": "text",
        "input_sha256": "sha",
        "output": "object",
        "duration_ms": "ms",
    },
    "tool_call": {"call_id": "id", "tool": "text", "skill_id": "id?", "arguments": "object"},
    "tool_result": {
        "call_id": "id",
        "status": "tool_status",
        "result": "object",
        "diagnostics": "object",
        "duration_ms": "ms",
    },
    "outcome": {"primary": "text", "progress": "text"},
    "run_finished": {"status": "terminal"},
}

_MANIFEST_IDENTITY_FIELDS = (
    "contract_version",
    "run_id",
    "task_id",
    "candidate_id",
    "skill_versions",
    "model_versions",
    "limits",
    "origin",
)
_MANIFEST_FIELDS = frozenset(
    _MANIFEST_IDENTITY_FIELDS + ("event_count", "head_sha256", "replayable", "non_replayable")
)
_EVENT_KEYS = frozenset(
    {"seq", "kind", "monotonic_ms", "data", "redacted_paths", "prev_sha256", "sha256"}
)
_MAX_MS = 9007199254740991
_MAX_TEXT = 128


class EvidenceError(ValueError):
    """Evidence was rejected. ``reason`` is a fixed code; messages never echo content."""

    REASONS = frozenset(
        {
            "malformed",
            "oversized",
            "version_mismatch",
            "truncated",
            "reordered",
            "corrupt",
            "incomplete",
            "redaction_inconsistent",
            "recorder_state",
        }
    )

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in self.REASONS:
            raise AssertionError("unknown evidence reason")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class Limits:
    """Storage bounds. Exceeding any bound is an error; nothing is silently dropped."""

    max_events: int = 4096
    max_event_bytes: int = 65536
    max_bundle_bytes: int = 4 * 1024 * 1024

    def as_dict(self) -> dict[str, int]:
        return {
            "max_events": self.max_events,
            "max_event_bytes": self.max_event_bytes,
            "max_bundle_bytes": self.max_bundle_bytes,
        }


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise EvidenceError("malformed", "value is not JSON-serializable") from None


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _chain(prev: str, event_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev + canonical_json(event_without_hash)).encode("ascii")).hexdigest()


def _genesis(manifest: Mapping[str, Any]) -> str:
    return sha256_of(
        {"log_version": LOG_VERSION, **{k: manifest[k] for k in _MANIFEST_IDENTITY_FIELDS}}
    )


def _redact(value: Any, keys: frozenset[str], path: str, found: list[str]) -> Any:
    if isinstance(value, dict):
        if set(value) == {REDACTION_MARKER}:
            found.append(path)  # already redacted upstream (e.g. replayed evidence); keep it
            return value
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise EvidenceError("malformed", "object keys must be strings")
            sub = f"{path}.{k}" if path else k
            if k.lower() in keys:
                out[k] = {REDACTION_MARKER: "sensitive_key"}
                found.append(sub)
            else:
                out[k] = _redact(v, keys, sub, found)
        return out
    if isinstance(value, list):
        return [_redact(v, keys, f"{path}[{i}]", found) for i, v in enumerate(value)]
    return value


def _find_markers(value: Any, path: str, found: list[str]) -> None:
    if isinstance(value, dict):
        if set(value) == {REDACTION_MARKER}:
            found.append(path)
            return
        for k, v in value.items():
            _find_markers(v, f"{path}.{k}" if path else k, found)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _find_markers(v, f"{path}[{i}]", found)


def _blocks_replay(kind: str, path: str) -> bool:
    for field in REPLAY_REQUIRED_FIELDS.get(kind, ()):
        if path == field or path.startswith(field + ".") or path.startswith(field + "["):
            return True
    return False


def _non_replayable(events: Iterable[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for event in events:
        for path in event["redacted_paths"]:
            if _blocks_replay(event["kind"], path):
                reasons.append(f"required_input_redacted:seq={event['seq']}:{path}")
    return reasons


class DecisionSource(Protocol):
    """Supplies one decision for a request. Implementations decide whether a provider is used."""

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]: ...


class ToolResult(NamedTuple):
    status: str
    result: dict[str, Any]
    diagnostics: dict[str, Any]


class ToolExecutor(Protocol):
    """Executes one declared tool call (an environment, a fake, or recorded results)."""

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult: ...


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise EvidenceError("malformed", f"{what} is not a valid identifier")
    return value


def _check_versions(value: Any, what: str, key_check: Callable[[Any], bool]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvidenceError("malformed", f"{what} must be an object")
    for k, v in value.items():
        if not key_check(k) or not isinstance(v, str) or not 0 < len(v) <= _MAX_TEXT:
            raise EvidenceError("malformed", f"{what} has an invalid entry")
    return dict(value)


class EventRecorder:
    """Records one run. Use the typed methods, then ``finish`` to obtain a validated bundle."""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        candidate_id: str | None,
        skill_versions: Mapping[str, str],
        model_versions: Mapping[str, str],
        limits: Limits | None = None,
        clock: Callable[[], int] | None = None,
        sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
        origin: Mapping[str, Any] | None = None,
    ) -> None:
        self._limits = limits or Limits()
        self._manifest: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "run_id": _check_id(run_id, "run_id"),
            "task_id": _check_id(task_id, "task_id"),
            "candidate_id": None if candidate_id is None else _check_id(candidate_id, "candidate"),
            "skill_versions": _check_versions(dict(skill_versions), "skill_versions", _is_id),
            "model_versions": _check_versions(
                dict(model_versions), "model_versions", lambda k: k in ROLES
            ),
            "limits": self._limits.as_dict(),
            "origin": dict(origin) if origin is not None else {"mode": "original"},
        }
        self._clock = clock or _monotonic_ms
        self._keys = frozenset(k.lower() for k in sensitive_keys)
        self._events: list[dict[str, Any]] = []
        self._head = _genesis(self._manifest)
        self._bytes = len(canonical_json(self._manifest))
        self._open_calls: dict[str, int] = {}
        self._call_ids: set[str] = set()
        self._state = "new"
        self._append("run_started", {})
        self._state = "recording"

    def _require(self, *states: str) -> None:
        if self._state not in states:
            raise EvidenceError("recorder_state", "operation not allowed in this recorder state")

    def _append(self, kind: str, data: dict[str, Any]) -> None:
        if len(self._events) >= self._limits.max_events:
            self._state = "failed"
            raise EvidenceError("oversized", "event count limit reached")
        found: list[str] = []
        try:
            clean = _redact(copy.deepcopy(data), self._keys, "", found)
        except RecursionError:
            self._state = "failed"
            raise EvidenceError("malformed", "event data exceeds supported nesting") from None
        now = self._clock()
        if not isinstance(now, int) or now < 0 or now > _MAX_MS:
            self._state = "failed"
            raise EvidenceError("malformed", "clock returned an invalid time")
        if self._events and now < self._events[-1]["monotonic_ms"]:
            self._state = "failed"
            raise EvidenceError("reordered", "clock moved backwards")
        event: dict[str, Any] = {
            "seq": len(self._events),
            "kind": kind,
            "monotonic_ms": now,
            "data": clean,
            "redacted_paths": found,
            "prev_sha256": self._head,
        }
        size = len(canonical_json(event)) + 80
        if size > self._limits.max_event_bytes:
            self._state = "failed"
            raise EvidenceError("oversized", "event exceeds the per-event byte limit")
        if self._bytes + size > self._limits.max_bundle_bytes:
            self._state = "failed"
            raise EvidenceError("oversized", "bundle byte limit reached")
        event["sha256"] = _chain(self._head, event)
        try:
            _check_event_shape(event, event["seq"])
        except EvidenceError:
            self._state = "failed"
            raise
        self._head = event["sha256"]
        self._bytes += size
        self._events.append(event)

    def observation(
        self, observation_id: str, source: str, captured_at_ms: int, content: Any
    ) -> None:
        """Record an observation identity and content digest; raw content is not stored."""
        self._require("recording")
        self._append(
            "observation",
            {
                "observation_id": observation_id,
                "source": source,
                "captured_at_ms": captured_at_ms,
                "content_sha256": sha256_of(content),
            },
        )

    def decide(
        self, source: DecisionSource, decision_id: str, role: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Obtain a decision from ``source``, record it with its timing, and return it."""
        self._require("recording")
        if role not in self._manifest["model_versions"]:
            raise EvidenceError("version_mismatch", "role has no declared model version")
        input_sha = sha256_of(request)
        start = self._clock()
        output = source.decide(decision_id, role, request)
        duration = self._clock() - start
        if not isinstance(output, dict):
            raise EvidenceError("malformed", "decision output must be an object")
        self._append(
            "decision",
            {
                "decision_id": decision_id,
                "role": role,
                "model": self._manifest["model_versions"][role],
                "input_sha256": input_sha,
                "output": output,
                "duration_ms": duration,
            },
        )
        return copy.deepcopy(output)

    def tool_call(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None = None
    ) -> None:
        self._require("recording")
        if call_id in self._call_ids:
            raise EvidenceError("malformed", "duplicate call_id")
        if skill_id is not None and skill_id not in self._manifest["skill_versions"]:
            raise EvidenceError("version_mismatch", "skill has no declared version")
        self._append(
            "tool_call",
            {"call_id": call_id, "tool": tool, "skill_id": skill_id, "arguments": arguments},
        )
        self._call_ids.add(call_id)
        self._open_calls[call_id] = self._events[-1]["monotonic_ms"]

    def tool_result(
        self,
        call_id: str,
        status: str,
        result: dict[str, Any],
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        self._require("recording")
        if call_id not in self._open_calls:
            raise EvidenceError("reordered", "tool result without an open tool call")
        started = self._open_calls.pop(call_id)
        self._append(
            "tool_result",
            {
                "call_id": call_id,
                "status": status,
                "result": result,
                "diagnostics": diagnostics or {},
                "duration_ms": max(0, self._clock() - started),
            },
        )

    def call_tool(
        self,
        tools: ToolExecutor,
        call_id: str,
        tool: str,
        arguments: dict[str, Any],
        skill_id: str | None = None,
    ) -> ToolResult:
        """Record a tool call, execute it through ``tools``, record and return its result."""
        self.tool_call(call_id, tool, arguments, skill_id)
        outcome = tools.execute(call_id, tool, copy.deepcopy(arguments), skill_id)
        self.tool_result(call_id, outcome.status, outcome.result, outcome.diagnostics)
        return outcome

    def outcome(self, primary: str, progress: str) -> None:
        """Record the independently evaluated outcome once, after all tool calls complete."""
        self._require("recording")
        if self._open_calls:
            raise EvidenceError("incomplete", "tool calls are still open")
        self._append("outcome", {"primary": primary, "progress": progress})
        self._state = "outcome"

    def finish(self, status: str) -> dict[str, Any]:
        """Close the log and return a validated bundle. Requires a recorded outcome."""
        self._require("outcome")
        self._append("run_finished", {"status": status})
        self._state = "finished"
        reasons = _non_replayable(self._events)
        bundle = {
            "log_version": LOG_VERSION,
            "manifest": {
                **self._manifest,
                "event_count": len(self._events),
                "head_sha256": self._head,
                "replayable": not reasons,
                "non_replayable": reasons,
            },
            "events": self._events,
        }
        validate_bundle(bundle)
        return copy.deepcopy(bundle)


def _check_field(kind: str, value: Any, path: str) -> None:
    ok: bool
    if kind == "id":
        ok = _is_id(value)
    elif kind == "id?":
        ok = value is None or _is_id(value)
    elif kind == "text":
        ok = isinstance(value, str) and 0 < len(value) <= _MAX_TEXT
    elif kind == "ms":
        ok = type(value) is int and 0 <= value <= _MAX_MS
    elif kind == "sha":
        ok = isinstance(value, str) and bool(SHA256_PATTERN.match(value))
    elif kind == "object":
        ok = isinstance(value, dict)
    elif kind == "role":
        ok = value in ROLES
    elif kind == "tool_status":
        ok = value in TOOL_RESULT_STATUSES
    elif kind == "terminal":
        ok = value in TERMINAL_STATUSES
    else:  # pragma: no cover - table is static
        raise AssertionError("unknown field kind")
    if not ok:
        raise EvidenceError("malformed", f"event field {path} is invalid")


def _check_event_shape(event: Any, index: int) -> None:
    if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
        raise EvidenceError("malformed", f"event {index} has the wrong fields")
    if type(event["seq"]) is not int or event["seq"] < 0:
        raise EvidenceError("malformed", f"event {index} has an invalid seq")
    if event["kind"] not in _EVENT_FIELDS:
        raise EvidenceError("malformed", f"event {index} has an unknown kind")
    _check_field("ms", event["monotonic_ms"], f"{index}.monotonic_ms")
    _check_field("sha", event["prev_sha256"], f"{index}.prev_sha256")
    _check_field("sha", event["sha256"], f"{index}.sha256")
    paths = event["redacted_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise EvidenceError("malformed", f"event {index} has invalid redacted_paths")
    spec = _EVENT_FIELDS[event["kind"]]
    data = event["data"]
    if not isinstance(data, dict) or set(data) != set(spec):
        raise EvidenceError("malformed", f"event {index} data has the wrong fields")
    for name, field_kind in spec.items():
        _check_field(field_kind, data[name], f"{index}.{name}")


def _check_manifest(manifest: Any) -> Limits:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise EvidenceError("malformed", "manifest has the wrong fields")
    if manifest["contract_version"] != CONTRACT_VERSION:
        raise EvidenceError("version_mismatch", "unsupported contract_version")
    _check_id(manifest["run_id"], "run_id")
    _check_id(manifest["task_id"], "task_id")
    if manifest["candidate_id"] is not None:
        _check_id(manifest["candidate_id"], "candidate_id")
    _check_versions(manifest["skill_versions"], "skill_versions", _is_id)
    _check_versions(manifest["model_versions"], "model_versions", lambda k: k in ROLES)
    limits = manifest["limits"]
    if (
        not isinstance(limits, dict)
        or set(limits) != {"max_events", "max_event_bytes", "max_bundle_bytes"}
        or not all(type(v) is int and v > 0 for v in limits.values())
    ):
        raise EvidenceError("malformed", "manifest limits are invalid")
    origin = manifest["origin"]
    if origin != {"mode": "original"} and not (
        isinstance(origin, dict)
        and set(origin) == {"mode", "source_head_sha256"}
        and origin["mode"] == "recorded_decision"
        and isinstance(origin["source_head_sha256"], str)
        and SHA256_PATTERN.match(origin["source_head_sha256"])
    ):
        raise EvidenceError("malformed", "manifest origin is invalid")
    if type(manifest["event_count"]) is not int or manifest["event_count"] < 0:
        raise EvidenceError("malformed", "manifest event_count is invalid")
    _check_field("sha", manifest["head_sha256"], "manifest.head_sha256")
    if type(manifest["replayable"]) is not bool or not isinstance(manifest["non_replayable"], list):
        raise EvidenceError("malformed", "manifest replayability is invalid")
    return Limits(**limits)


def validate_bundle(bundle: Any, limits: Limits | None = None) -> None:
    """Validate a bundle; raise ``EvidenceError`` with a fixed reason on the first failure.

    Check order: structure and versions, size bounds, sequence order, truncation, checksum chain,
    ordering semantics, completeness, then redaction/replayability consistency.
    """
    if not isinstance(bundle, dict) or set(bundle) != {"log_version", "manifest", "events"}:
        raise EvidenceError("malformed", "bundle must have log_version, manifest and events")
    if bundle["log_version"] != LOG_VERSION:
        raise EvidenceError("version_mismatch", "unsupported log_version")
    declared = _check_manifest(bundle["manifest"])
    bound = limits or Limits()
    effective = Limits(
        max_events=min(bound.max_events, declared.max_events),
        max_event_bytes=min(bound.max_event_bytes, declared.max_event_bytes),
        max_bundle_bytes=min(bound.max_bundle_bytes, declared.max_bundle_bytes),
    )
    manifest = bundle["manifest"]
    events = bundle["events"]
    if not isinstance(events, list):
        raise EvidenceError("malformed", "events must be a list")
    if len(events) > effective.max_events:
        raise EvidenceError("oversized", "too many events")
    if len(canonical_json(bundle)) > effective.max_bundle_bytes:
        raise EvidenceError("oversized", "bundle exceeds the byte limit")
    for i, event in enumerate(events):
        _check_event_shape(event, i)
        if len(canonical_json(event)) > effective.max_event_bytes:
            raise EvidenceError("oversized", f"event {i} exceeds the byte limit")

    seqs = [e["seq"] for e in events]
    if seqs != list(range(len(events))):
        if sorted(seqs) == list(range(len(events))):
            raise EvidenceError("reordered", "events are out of sequence order")
        if all(a < b for a, b in zip(seqs, seqs[1:], strict=False)):
            raise EvidenceError("truncated", "events are missing from the sequence")
        raise EvidenceError("corrupt", "event sequence numbers are inconsistent")
    if len(events) < manifest["event_count"] or not events:
        raise EvidenceError("truncated", "fewer events than the manifest declares")
    if len(events) > manifest["event_count"]:
        raise EvidenceError("corrupt", "more events than the manifest declares")
    if events[-1]["kind"] != "run_finished":
        raise EvidenceError("truncated", "log does not end with run_finished")

    prev = _genesis(manifest)
    for event in events:
        body = {k: v for k, v in event.items() if k != "sha256"}
        if event["prev_sha256"] != prev or _chain(prev, body) != event["sha256"]:
            raise EvidenceError("corrupt", f"checksum mismatch at seq {event['seq']}")
        prev = event["sha256"]
    if prev != manifest["head_sha256"]:
        raise EvidenceError("corrupt", "head checksum does not match the manifest")

    _check_semantics(events, manifest)

    for event in events:
        found: list[str] = []
        try:
            _find_markers(event["data"], "", found)
        except RecursionError:
            raise EvidenceError("malformed", "event data exceeds supported nesting") from None
        if sorted(found) != sorted(event["redacted_paths"]):
            raise EvidenceError(
                "redaction_inconsistent", f"undeclared or missing redaction at seq {event['seq']}"
            )
    reasons = _non_replayable(events)
    if manifest["replayable"] != (not reasons) or manifest["non_replayable"] != reasons:
        raise EvidenceError(
            "redaction_inconsistent", "manifest replayability contradicts recorded redactions"
        )


def _check_semantics(events: list[dict[str, Any]], manifest: Mapping[str, Any]) -> None:
    for a, b in zip(events, events[1:], strict=False):
        if b["monotonic_ms"] < a["monotonic_ms"]:
            raise EvidenceError("reordered", f"time decreases at seq {b['seq']}")
    kinds = [e["kind"] for e in events]
    if kinds[0] != "run_started" or kinds.count("run_started") != 1:
        raise EvidenceError("reordered", "run_started must be first and unique")
    if kinds.count("run_finished") != 1:
        raise EvidenceError("reordered", "run_finished must be last and unique")
    if kinds.count("outcome") != 1:
        raise EvidenceError("incomplete", "exactly one outcome is required")
    if kinds[-2] != "outcome":
        raise EvidenceError("reordered", "outcome must immediately precede run_finished")
    open_calls: set[str] = set()
    seen_calls: set[str] = set()
    for event in events:
        data = event["data"]
        if event["kind"] == "tool_call":
            if data["call_id"] in seen_calls:
                raise EvidenceError("corrupt", "duplicate call_id")
            if data["skill_id"] is not None and data["skill_id"] not in manifest["skill_versions"]:
                raise EvidenceError("version_mismatch", "tool call names an undeclared skill")
            seen_calls.add(data["call_id"])
            open_calls.add(data["call_id"])
        elif event["kind"] == "tool_result":
            if data["call_id"] not in open_calls:
                raise EvidenceError("reordered", "tool result precedes or lacks its call")
            open_calls.discard(data["call_id"])
        elif event["kind"] == "decision":
            if manifest["model_versions"].get(data["role"]) != data["model"]:
                raise EvidenceError("version_mismatch", "decision model differs from manifest")
        elif event["kind"] == "outcome" and open_calls:
            raise EvidenceError("incomplete", "tool calls lack results before the outcome")


def load_bundle(path: str | Path, limits: Limits | None = None) -> dict[str, Any]:
    """Read and validate a bundle file without reading more than the byte limit."""
    bound = limits or Limits()
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(bound.max_bundle_bytes + 1)
    except FileNotFoundError:
        raise EvidenceError("malformed", "evidence file not found") from None
    except OSError:
        raise EvidenceError("malformed", "evidence file is unreadable") from None
    if len(raw) > bound.max_bundle_bytes:
        raise EvidenceError("oversized", "evidence file exceeds the byte limit")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise EvidenceError("corrupt", "evidence file is not canonical ASCII JSON") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.pos >= len(text.rstrip()) or exc.msg.startswith("Unterminated string"):
            raise EvidenceError("truncated", "evidence file ends unexpectedly") from None
        raise EvidenceError("corrupt", "evidence file is not valid JSON") from None
    except RecursionError:
        raise EvidenceError("malformed", "evidence file exceeds supported nesting") from None
    validate_bundle(data, bound)
    if not isinstance(data, dict):  # pragma: no cover - validate_bundle guarantees this
        raise EvidenceError("malformed", "bundle must be an object")
    return data


def write_bundle(bundle: Mapping[str, Any], path: str | Path) -> None:
    """Validate and write a bundle as canonical JSON. Refuses to overwrite an existing file."""
    validate_bundle(bundle)
    text = canonical_json(bundle)
    try:
        with Path(path).open("x", encoding="ascii") as handle:
            handle.write(text)
    except FileExistsError:
        raise EvidenceError("malformed", "refusing to overwrite existing evidence") from None
    except OSError:
        raise EvidenceError("malformed", "evidence destination is not writable") from None
