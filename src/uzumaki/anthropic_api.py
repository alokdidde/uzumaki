"""Live model provider for the Anthropic Messages API (Claude, called directly).

``AnthropicProvider`` implements the provider interface of ``uzumaki.gateway.GatewayProvider``
with the same provider-neutral requests and results, against ``POST {base_url}/v1/messages``.
The runtime is stdlib-only, so this is raw HTTP rather than the ``anthropic`` SDK.

- Key: read from ``ANTHROPIC_API_KEY`` (or the variable named by ``key_env``) at send time, sent
  as ``x-api-key``; never stored in results, errors or evidence.
- Request: ``model``, ``system``, ``messages`` and ``max_tokens``. Sampling parameters are not
  sent (current models reject them). Current models think adaptively and thinking counts toward
  ``max_tokens``, so at least ``MIN_MAX_TOKENS`` is requested; the run's output budgets still
  bound what is accepted.
- Result: the text blocks joined, ``stop_reason`` as ``finish_reason``, usage from
  ``input_tokens``/``output_tokens``. A refusal or an answer without text returns
  ``{"error": ...}``, which the caller's validator treats as invalid output.
- Errors, all retried within budget: 429 and 529 (overloaded) raise ``RateLimited``; a connection
  dropped mid-reply raises ``ProviderDisconnected``; other 5xx and connection failures raise
  ``TransientProviderError``; a socket timeout is ``ProviderTimeout``. Not retried: 401/403 and
  other client errors raise ``ProviderRejected`` with fixed codes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
from collections.abc import Callable
from typing import Any

from uzumaki.faults import (
    ProviderDisconnected,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)
from uzumaki.gateway import (
    DISCONNECTS,
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_S,
    ProviderRejected,
    Transport,
    _check_request,
    _secure_or_local,
    urllib_transport,
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
API_VERSION = "2023-06-01"
MIN_MAX_TOKENS = 4096


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("input_tokens"), raw.get("output_tokens")
    if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
        return {"input_tokens": prompt, "output_tokens": completion}
    return None


def _parse(data: bytes) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8"))
        blocks = document["content"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"error": "schema"}
    if not isinstance(blocks, list):
        return {"error": "schema"}
    stop = document.get("stop_reason")
    if stop == "refusal":
        return {"error": "refusal"}
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    if not texts:
        return {"error": "schema"}
    result: dict[str, Any] = {
        "text": "".join(texts),
        "model": document.get("model") if isinstance(document.get("model"), str) else None,
        "finish_reason": stop if isinstance(stop, str) else None,
    }
    usage = _usage(document.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return result


class AnthropicProvider:
    """One live Messages API adapter; see the module docstring for the contract."""

    def __init__(
        self,
        *,
        base_url: str = ANTHROPIC_BASE_URL,
        key_env: str = ANTHROPIC_KEY_ENV,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _secure_or_local(base_url):
            raise ValueError("Anthropic base URL must use https (plain http only to localhost)")
        self.url = base_url.rstrip("/") + "/v1/messages"
        self.key_env = key_env
        self._transport = transport or urllib_transport
        self._clock = clock

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, int(MAX_TIMEOUT_S * 1000))

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        _check_request(request)
        key = os.environ.get(self.key_env)
        if not key:
            raise ProviderRejected("missing_api_key")
        body = json.dumps(
            {
                "model": request["model"],
                "max_tokens": max(request["max_tokens"], MIN_MAX_TOKENS),
                "system": request["system"],
                "messages": request["messages"],
            }
        ).encode("utf-8")
        headers = {
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
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
        if status in (429, 529):  # 529: overloaded
            raise RateLimited("rate_limited")
        if status >= 500:
            raise TransientProviderError("provider_unavailable")
        if status in (401, 403):
            raise ProviderRejected("authentication_failed")
        if status != 200:
            raise ProviderRejected("request_rejected")
        if len(data) > MAX_RESPONSE_BYTES:
            return {"error": "oversized", "latency_ms": latency_ms}
        result = _parse(data)
        result["latency_ms"] = latency_ms
        return result
