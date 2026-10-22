"""Live model provider for any OpenAI-compatible chat-completions endpoint (Vercel AI Gateway by
default; also OpenAI, OpenRouter, a local Ollama or vLLM server; see ``uzumaki.backends``).

``GatewayProvider`` implements the ``uzumaki.supervisor`` provider interface, so every live call
runs inside a ``BoundedRun``: the run counts model calls, enforces token budgets from reported
usage only, and retries what this module classifies as transient. Requests are provider-neutral::

    {"role": "planner", "model": "anthropic/claude-sonnet-5.5", "prompt_version": "planner-v1",
     "system": "...", "messages": [{"role": "user", "content": "..."}], "max_tokens": 600}

and results are::

    {"text": "...", "model": "...", "finish_reason": "stop", "latency_ms": 812,
     "usage": {"input_tokens": 950, "output_tokens": 61}}

``usage`` is omitted when the gateway does not report both token counts, so unknown usage stays
unknown. Classification (all three transient kinds are retried within budget): HTTP 429 raises
``RateLimited``; a connection dropped mid-reply raises ``ProviderDisconnected``; 5xx and other
connection failures raise ``TransientProviderError``; a socket timeout raises ``ProviderTimeout``;
authentication and other client errors raise ``ProviderRejected`` (not retried, fails the run); a
response that does not have the expected shape returns ``{"error": "schema"}``, which the caller's
validator treats as invalid output.
Requests ask for ``temperature`` 0 and ``max_tokens``; with ``reasoning=True`` (OpenAI's own API,
whose reasoning models reject both) they send ``max_completion_tokens`` instead, at least
``MIN_REASONING_TOKENS`` because reasoning counts toward it.

The API key is read from the environment variable named by ``key_env`` (``AI_GATEWAY_API_KEY``)
when a request is sent; ``key_env=None`` sends no key (local servers). Base URLs must use https,
except plain http to the local host. The key is never stored in results, errors or evidence, and
errors carry only fixed codes. Nothing here runs in the default offline checks: tests inject
``transport``.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from uzumaki.faults import (
    ProviderDisconnected,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)

DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh/v1"
DEFAULT_KEY_ENV = "AI_GATEWAY_API_KEY"
#: Gateway model ID for the protocol's candidate model (``claude-sonnet-5-5``); unverified until a
#: live smoke run confirms the gateway serves it under this name.
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
MIN_REASONING_TOKENS = 4096
MAX_RESPONSE_BYTES = 1 << 20
MAX_TIMEOUT_S = 60.0
#: Failures that mean the connection closed before a complete reply.
DISCONNECTS = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    http.client.IncompleteRead,
)
REQUEST_KEYS = frozenset({"role", "model", "prompt_version", "system", "messages", "max_tokens"})

#: ``transport(url, headers, body, timeout_s) -> (http_status, response_bytes)``.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


class ProviderRejected(Exception):
    """The gateway refused the request (missing key, authentication, bad request). Fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout_s: float
) -> tuple[int, bytes]:
    """POST with the standard library; HTTP errors are returned as statuses, not raised."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(MAX_RESPONSE_BYTES + 1)


def _secure_or_local(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")


def _check_request(request: dict[str, Any]) -> None:
    if set(request) != REQUEST_KEYS:
        raise ValueError("model request has missing or unknown fields")
    messages = request["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("model request needs at least one message")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("user", "assistant")
            or not isinstance(message["content"], str)
        ):
            raise ValueError("model request message is malformed")
    if not isinstance(request["system"], str) or not isinstance(request["model"], str):
        raise ValueError("model request system prompt or model is malformed")
    if type(request["max_tokens"]) is not int or not 1 <= request["max_tokens"] <= 8192:
        raise ValueError("model request max_tokens is out of range")


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
        return {"input_tokens": prompt, "output_tokens": completion}
    return None


def _parse(data: bytes) -> dict[str, Any] | None:
    try:
        document = json.loads(data.decode("utf-8"))
        choice = document["choices"][0]
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(text, str):
        return None
    result: dict[str, Any] = {
        "text": text,
        "model": document.get("model") if isinstance(document.get("model"), str) else None,
        "finish_reason": choice.get("finish_reason")
        if isinstance(choice.get("finish_reason"), str)
        else None,
    }
    usage = _usage(document.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return result


class GatewayProvider:
    """One live chat-completions adapter; see the module docstring for the contract."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        key_env: str | None = DEFAULT_KEY_ENV,
        reasoning: bool = False,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _secure_or_local(base_url):
            raise ValueError("gateway base URL must use https (plain http only to localhost)")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key_env = key_env
        self.reasoning = reasoning
        self._transport = transport or urllib_transport
        self._clock = clock

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, int(MAX_TIMEOUT_S * 1000))

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        _check_request(request)
        key = os.environ.get(self.key_env) if self.key_env is not None else None
        if self.key_env is not None and not key:
            raise ProviderRejected("missing_api_key")
        payload: dict[str, Any] = {
            "model": request["model"],
            "messages": [{"role": "system", "content": request["system"]}] + request["messages"],
        }
        if self.reasoning:
            # Reasoning models reject temperature and max_tokens, and reasoning counts toward
            # max_completion_tokens, so ask for at least MIN_REASONING_TOKENS.
            payload["max_completion_tokens"] = max(request["max_tokens"], MIN_REASONING_TOKENS)
        else:
            payload["max_tokens"] = request["max_tokens"]
            payload["temperature"] = 0
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        timeout_s = max(1.0, min(MAX_TIMEOUT_S, timeout_ms / 1000))
        started = self._clock()
        try:
            status, data = self._transport(self.url, headers, body, timeout_s)
        except TimeoutError:
            raise ProviderTimeout("provider_timeout") from None
        except DISCONNECTS:
            raise ProviderDisconnected("provider_disconnected") from None
        except (urllib.error.URLError, ConnectionError, OSError):
            raise TransientProviderError("provider_unreachable") from None
        latency_ms = round((self._clock() - started) * 1000)
        if status == 429:
            raise RateLimited("rate_limited")
        if status >= 500:
            raise TransientProviderError("provider_unavailable")
        if status in (401, 403):
            raise ProviderRejected("authentication_failed")
        if status != 200:
            raise ProviderRejected("request_rejected")
        if len(data) > MAX_RESPONSE_BYTES:
            return {"error": "oversized", "latency_ms": latency_ms}
        parsed = _parse(data)
        if parsed is None:
            return {"error": "schema", "latency_ms": latency_ms}
        parsed["latency_ms"] = latency_ms
        return parsed


class RecordingProvider:
    """Wraps a provider and keeps every successful exchange as a ``RecordedProvider`` record, so a
    live run can be replayed offline with ``uzumaki.providers.RecordedProvider``. Latency is kept;
    the key never reaches a request or result, so records hold none."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.records: list[dict[str, Any]] = []

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self.inner.request(request)
        self.records.append({"request": request, "result": result})
        return result

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        if hasattr(self.inner, "request_within"):
            result: dict[str, Any] = self.inner.request_within(request, timeout_ms)
        else:
            result = self.inner.request(request)
        self.records.append({"request": request, "result": result})
        return result

    def document(self) -> dict[str, Any]:
        """The recorded exchanges as a ``RecordedProvider`` file (last answer wins per request)."""
        unique: dict[str, dict[str, Any]] = {}
        for record in self.records:
            unique[json.dumps(record["request"], sort_keys=True)] = record
        return {"schema_version": "1.0", "records": list(unique.values())}
