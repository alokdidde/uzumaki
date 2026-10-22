"""One ``model_provenance`` record per model call, failed calls included.

``ProvenanceLog`` wraps any provider (``uzumaki.backends``) and records each call as a contract
v1.0 ``model_provenance`` record (``docs/contracts/v1.json``): the provider name and requested
model, the model the provider reported, the role, live or recorded, SHA-256 digests of the prompt
template (``prompt_version`` and ``system``), the request messages and the result, when the call
started, its latency and the usage the provider reported. Usage the provider did not report stays
``null`` (``unknown``), never zero. Records hold digests, IDs, numbers and fixed codes only, never
prompt, context or reply text, and never a key.

A call that failed has ``output_sha256`` null; its fixed error code is kept next to the record
(``ProvenanceLog.calls``), since the contract record has no field for it. Retries are the
caller's (``uzumaki.supervisor.BoundedRun``), so a retried call gives one record per attempt.
``document()`` is the file the model commands write as ``model-provenance.json``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from uzumaki.evidence import EvidenceError, sha256_of
from uzumaki.faults import (
    ProviderDisconnected,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
    WorkerFailure,
)

LOG_FORMAT = "uzumaki.model-provenance-log/1.0"
ROLES = ("planner", "generator")
REPLAY_MODES = ("live", "recorded")
_MAX_NAME = 64
_MAX_MODEL = 128


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def error_code(error: BaseException) -> str:
    """A fixed code for a provider failure; never the exception's message."""
    if isinstance(error, ProviderTimeout):
        return "provider_timeout"
    if isinstance(error, RateLimited):
        return "rate_limited"
    if isinstance(error, ProviderDisconnected):
        return "provider_disconnected"
    if isinstance(error, TransientProviderError):
        return "provider_transient"
    if isinstance(error, WorkerFailure):
        return "worker_failure"
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.isidentifier() and len(code) <= _MAX_NAME:
        return code
    return "provider_error"


def _usage(result: Any) -> dict[str, Any]:
    raw = result.get("usage") if isinstance(result, dict) else None
    counts: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    if isinstance(raw, dict):
        for name in counts:
            value = raw.get(name)
            if type(value) is int and value >= 0:
                counts[name] = value
    known = sum(value is not None for value in counts.values())
    status = ("unknown", "partially_reported", "reported")[known]
    return {"status": status, **counts, "cost_usd": None}


def _short(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _digest(value: Any) -> str | None:
    try:
        return sha256_of(value)
    except EvidenceError:
        return None


class ProvenanceLog:
    """Wraps a provider and keeps one ``model_provenance`` record per call."""

    def __init__(
        self,
        inner: Any,
        provider: str,
        *,
        replay_mode: str = "live",
        clock: Callable[[], int] = _epoch_ms,
        timer: Callable[[], int] = _monotonic_ms,
    ) -> None:
        if not isinstance(provider, str) or not 0 < len(provider) <= _MAX_NAME:
            raise ValueError("provider name must be 1 to 64 characters")
        if replay_mode not in REPLAY_MODES:
            raise ValueError(f"replay_mode must be one of {REPLAY_MODES}")
        self.inner = inner
        self.provider = provider
        self.replay_mode = replay_mode
        self._clock = clock
        self._timer = timer
        #: ``{"record": <model_provenance>, "error": <code or None>}`` per call, in order.
        self.calls: list[dict[str, Any]] = []

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._record(request, lambda: self.inner.request(request))

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        if hasattr(self.inner, "request_within"):
            return self._record(request, lambda: self.inner.request_within(request, timeout_ms))
        return self.request(request)

    def _record(self, request: dict[str, Any], call: Callable[[], Any]) -> dict[str, Any]:
        requested_at = self._clock()
        started = self._timer()
        result: Any = None
        error: str | None = None
        try:
            result = call()
        except Exception as failure:
            error = error_code(failure)
            raise
        finally:
            if error is None and isinstance(result, dict) and isinstance(result.get("error"), str):
                error = result["error"] if result["error"].isidentifier() else "invalid_output"
            elif error is None and not isinstance(result, dict):
                error = "invalid_output"
            self.calls.append(
                {
                    "record": self._provenance(request, result, requested_at, started),
                    "error": error,
                }
            )
        return cast(dict[str, Any], result)

    def _provenance(
        self, request: dict[str, Any], result: Any, requested_at: int, started: int
    ) -> dict[str, Any]:
        role = request.get("role") if isinstance(request, dict) else None
        model = request.get("model") if isinstance(request, dict) else None
        reported = result.get("model") if isinstance(result, dict) else None
        latency = result.get("latency_ms") if isinstance(result, dict) else None
        if type(latency) is not int or latency < 0:
            latency = max(0, self._timer() - started)
        template = {
            "prompt_version": request.get("prompt_version"),
            "system": request.get("system"),
        }
        return {
            "schema_version": "1.0",
            "kind": "model_provenance",
            "payload": {
                "provenance_id": f"{role if role in ROLES else 'model'}-{len(self.calls) + 1:04d}",
                "provider": self.provider,
                "model": str(model)[:_MAX_MODEL] or "unknown",
                "reported_model": reported if _short(reported, _MAX_MODEL) else None,
                "role": role if role in ROLES else "planner",
                "replay_mode": self.replay_mode,
                "prompt_sha256": _digest(template) or sha256_of(None),
                "input_sha256": _digest(request.get("messages")) or sha256_of(None),
                "output_sha256": _digest(result) if result is not None else None,
                "requested_at_ms": requested_at,
                "latency_ms": latency,
                "usage": _usage(result),
            },
        }

    def document(self) -> dict[str, Any]:
        """The log as written to ``model-provenance.json``."""
        return {"format": LOG_FORMAT, "calls": [dict(call) for call in self.calls]}
