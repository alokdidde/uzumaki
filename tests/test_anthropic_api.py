"""Anthropic Messages API adapter (``uzumaki.anthropic_api``) against an injected transport."""

import json
from typing import Any

import pytest

from uzumaki.anthropic_api import MIN_MAX_TOKENS, AnthropicProvider
from uzumaki.faults import ProviderTimeout, TransientProviderError
from uzumaki.gateway import ProviderRejected

KEY = "test-key-not-real-0123456789"
REQUEST = {
    "role": "planner",
    "model": "claude-opus-5-5",
    "prompt_version": "planner-v2",
    "system": "be brief",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 600,
}


def message(*blocks: Any, stop: str = "end_turn", usage: Any = None) -> bytes:
    document: dict[str, Any] = {"model": "claude-opus-5-5", "content": list(blocks)}
    document["stop_reason"] = stop
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode()


def text(value: str) -> dict[str, str]:
    return {"type": "text", "text": value}


class Transport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def __call__(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append((url, headers, json.loads(body), timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, tuple)
        return item


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)


def test_request_and_result_contract() -> None:
    usage = {"input_tokens": 40, "output_tokens": 7}
    transport = Transport((200, message(text("hi "), text("there"), usage=usage)))
    result = AnthropicProvider(transport=transport).request_within(REQUEST, 30_000)
    url, headers, body, timeout = transport.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert body == {
        "model": "claude-opus-5-5",
        "max_tokens": MIN_MAX_TOKENS,  # thinking counts toward max_tokens
        "system": "be brief",
        "messages": REQUEST["messages"],
    }
    assert timeout == 30.0
    assert result["text"] == "hi there" and result["finish_reason"] == "end_turn"
    assert result["usage"] == usage
    assert KEY not in json.dumps(result)


def test_thinking_blocks_are_skipped_and_odd_usage_stays_unknown() -> None:
    thinking = {"type": "thinking", "thinking": "...", "signature": "x"}
    transport = Transport((200, message(thinking, text("ok"), usage={"input_tokens": "1"})))
    result = AnthropicProvider(transport=transport).request(REQUEST)
    assert result["text"] == "ok" and "usage" not in result


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (message(text("no"), stop="refusal"), "refusal"),
        (message({"type": "thinking", "thinking": "..."}), "schema"),
        (b"not json", "schema"),
        (json.dumps({"content": "flat"}).encode(), "schema"),
    ],
)
def test_unusable_answers_are_invalid_output(data: bytes, error: str) -> None:
    result = AnthropicProvider(transport=Transport((200, data))).request(REQUEST)
    assert result["error"] == error


@pytest.mark.parametrize("status", [429, 500, 529])
def test_overload_and_server_errors_are_transient(status: int) -> None:
    with pytest.raises(TransientProviderError):
        AnthropicProvider(transport=Transport((status, b"{}"))).request(REQUEST)


@pytest.mark.parametrize(
    ("status", "code"), [(401, "authentication_failed"), (400, "request_rejected")]
)
def test_client_errors_are_rejected(status: int, code: str) -> None:
    with pytest.raises(ProviderRejected, match=code):
        AnthropicProvider(transport=Transport((status, b"{}"))).request(REQUEST)


def test_timeouts_connection_failures_and_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ProviderTimeout):
        AnthropicProvider(transport=Transport(TimeoutError())).request(REQUEST)
    with pytest.raises(TransientProviderError):
        AnthropicProvider(transport=Transport(ConnectionResetError())).request(REQUEST)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    transport = Transport()
    with pytest.raises(ProviderRejected, match="missing_api_key"):
        AnthropicProvider(transport=transport).request(REQUEST)
    assert transport.calls == []


def test_plain_http_only_to_localhost() -> None:
    with pytest.raises(ValueError):
        AnthropicProvider(base_url="http://example.com")
    assert AnthropicProvider(base_url="http://localhost:8080").url.startswith("http://localhost")
