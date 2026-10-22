"""Codex CLI adapter (``uzumaki.codex``) against an injected runner; no CLI, no network."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from uzumaki.codex import CODEX_DEFAULT_MODEL, CodexProvider
from uzumaki.faults import ProviderTimeout, TransientProviderError
from uzumaki.gateway import ProviderRejected

REQUEST = {
    "role": "planner",
    "model": CODEX_DEFAULT_MODEL,
    "prompt_version": "planner-v1",
    "system": "be brief",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 50,
}


def jsonl(*events: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def turn(text: str = "ok", usage: Any = None) -> bytes:
    events: list[dict[str, Any]] = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
    ]
    completed: dict[str, Any] = {"type": "turn.completed"}
    if usage is not None:
        completed["usage"] = usage
    events.append(completed)
    return jsonl(*events)


class Runner:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str, str, float]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes, cwd: str, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin.decode(), cwd, timeout_s))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[no-any-return]


def test_request_and_result_contract() -> None:
    runner = Runner((0, turn("plan", {"input_tokens": 9, "output_tokens": 2}), b""))
    clock = iter([1.0, 1.25])
    result = CodexProvider(runner=runner, clock=lambda: next(clock)).request_within(REQUEST, 5000)
    assert result == {
        "text": "plan",
        "model": CODEX_DEFAULT_MODEL,
        "finish_reason": "stop",
        "latency_ms": 250,
        "usage": {"input_tokens": 9, "output_tokens": 2},
    }
    argv, stdin, _, timeout_s = runner.calls[0]
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    assert "read-only" in argv and "--ephemeral" in argv and "--model" not in argv
    assert "be brief" in stdin and "hello" in stdin
    assert timeout_s == 5.0


def test_explicit_model_is_passed_to_the_cli() -> None:
    runner = Runner((0, turn(), b""))
    CodexProvider(runner=runner).request({**REQUEST, "model": "gpt-x"})
    argv = runner.calls[0][0]
    assert argv[argv.index("--model") + 1] == "gpt-x"


def test_unreported_usage_stays_unknown() -> None:
    result = CodexProvider(runner=Runner((0, turn(), b""))).request(REQUEST)
    assert "usage" not in result


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (TimeoutError(), ProviderTimeout, "provider_timeout"),
        (FileNotFoundError(), ProviderRejected, "missing_codex_cli"),
        ((1, b"", b"Error: not logged in, run codex login"), ProviderRejected, None),
        ((1, jsonl({"type": "turn.failed", "error": {"message": "429"}}), b""), None, None),
        ((0, jsonl({"type": "error", "message": "stream disconnected"}), b""), None, None),
    ],
)
def test_failures_are_classified(response: Any, error: Any, code: str | None) -> None:
    expected = error or TransientProviderError
    with pytest.raises(expected) as raised:
        CodexProvider(runner=Runner(response)).request(REQUEST)
    if code is not None:
        assert str(raised.value) == code
    if expected is TransientProviderError:
        assert not isinstance(raised.value, ProviderTimeout)


def test_output_without_a_final_message_is_invalid() -> None:
    result = CodexProvider(runner=Runner((0, b"not json\n", b""))).request(REQUEST)
    assert result["error"] == "schema"


def test_malformed_requests_are_refused_locally() -> None:
    runner = Runner()
    with pytest.raises(ValueError):
        CodexProvider(runner=runner).request({**REQUEST, "max_tokens": 0})
    assert runner.calls == []
