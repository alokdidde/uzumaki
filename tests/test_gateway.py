"""Live gateway adapter (``uzumaki.gateway``) against an injected transport; no network."""

import json
from typing import Any

import pytest

from uzumaki.faults import ProviderTimeout, TransientProviderError
from uzumaki.gateway import (
    DEFAULT_KEY_ENV,
    GatewayProvider,
    ProviderRejected,
    RecordingProvider,
)
from uzumaki.providers import RecordedProvider

KEY = "test-key-not-real-0123456789"
REQUEST = {
    "role": "planner",
    "model": "anthropic/claude-sonnet-5.5",
    "prompt_version": "planner-v1",
    "system": "be brief",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 50,
}


def completion(text: str = "ok", usage: Any = None) -> bytes:
    document: dict[str, Any] = {
        "model": "anthropic/claude-sonnet-5.5",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode()


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
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)


def test_request_and_result_contract() -> None:
    transport = Transport((200, completion("hi", {"prompt_tokens": 12, "completion_tokens": 3})))
    provider = GatewayProvider(transport=transport)
    result = provider.request_within(REQUEST, 30_000)
    url, headers, body, timeout = transport.calls[0]
    assert url == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "anthropic/claude-sonnet-5.5" and body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1:] == REQUEST["messages"] and body["max_tokens"] == 50
    assert timeout == 30.0
    assert result["text"] == "hi" and result["finish_reason"] == "stop"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize(
    "usage",
    [None, {"prompt_tokens": 12}, {"prompt_tokens": "12", "completion_tokens": 3}, "lots"],
)
def test_unreported_or_malformed_usage_stays_unknown(usage: Any) -> None:
    result = GatewayProvider(transport=Transport((200, completion("x", usage)))).request(REQUEST)
    assert "usage" not in result


def test_unknown_usage_fields_are_ignored() -> None:
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "cache_read_tokens": 9, "cost": "0.1"}
    result = GatewayProvider(transport=Transport((200, completion("x", usage)))).request(REQUEST)
    assert result["usage"] == {"input_tokens": 1, "output_tokens": 2}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ((429, b"slow down"), TransientProviderError),
        ((503, b""), TransientProviderError),
        (ConnectionResetError(), TransientProviderError),
        (TimeoutError(), ProviderTimeout),
    ],
)
def test_transient_failures_are_retryable(response: Any, error: type[Exception]) -> None:
    with pytest.raises(error) as raised:
        GatewayProvider(transport=Transport(response)).request(REQUEST)
    assert KEY not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "code"), [(401, "authentication_failed"), (400, "request_rejected")]
)
def test_client_errors_are_rejected_with_fixed_codes(status: int, code: str) -> None:
    with pytest.raises(ProviderRejected) as raised:
        GatewayProvider(transport=Transport((status, KEY.encode()))).request(REQUEST)
    assert raised.value.code == code and KEY not in str(raised.value)


def test_missing_key_is_rejected_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_KEY_ENV)
    transport = Transport()
    with pytest.raises(ProviderRejected, match="missing_api_key"):
        GatewayProvider(transport=transport).request(REQUEST)
    assert transport.calls == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"choices": []}', b'{"choices": [{"message": {"content": 7}}]}', b"[]"],
)
def test_schema_changes_are_reported_as_invalid_output(body: bytes) -> None:
    result = GatewayProvider(transport=Transport((200, body))).request(REQUEST)
    assert result["error"] == "schema" and "text" not in result


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"messages": []},
        {"messages": [{"role": "system", "content": "x"}]},
        {"max_tokens": 0},
    ],
)
def test_malformed_requests_are_refused_locally(change: dict[str, Any]) -> None:
    transport = Transport()
    with pytest.raises(ValueError):
        GatewayProvider(transport=transport).request({**REQUEST, **change})
    assert transport.calls == []


def test_only_https_gateways() -> None:
    with pytest.raises(ValueError):
        GatewayProvider(base_url="http://ai-gateway.vercel.sh/v1")


def test_recorded_exchanges_replay_offline(tmp_path: Any) -> None:
    transport = Transport((200, completion("first", {"prompt_tokens": 1, "completion_tokens": 1})))
    recording = RecordingProvider(GatewayProvider(transport=transport))
    live = recording.request(REQUEST)
    path = tmp_path / "exchanges.json"
    path.write_text(json.dumps(recording.document()))
    assert KEY not in path.read_text()
    assert RecordedProvider.from_path(path).request(REQUEST) == live
