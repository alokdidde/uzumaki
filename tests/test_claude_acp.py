"""Claude-subscription adapter (``uzumaki.claude_acp``) against a scripted local ACP agent."""

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from uzumaki.claude_acp import ACP_DEFAULT_MODEL, ClaudeAcpProvider, agent_command
from uzumaki.faults import ProviderDisconnected, ProviderTimeout, RateLimited
from uzumaki.gateway import ProviderRejected

AGENT = Path(__file__).with_name("fake_acp_agent.py")
REQUEST = {
    "role": "planner",
    "model": ACP_DEFAULT_MODEL,
    "prompt_version": "planner-v2",
    "system": "answer pong to ping",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 50,
}
ENVIRON = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "ANTHROPIC_API_KEY": "sk-ant-SECRET-0000"}


def provider(mode: str, environ: dict[str, str] | None = None) -> ClaudeAcpProvider:
    command = [sys.executable, "-I", str(AGENT), mode]
    return ClaudeAcpProvider(command, environ=ENVIRON if environ is None else environ)


def ask(mode: str, timeout_ms: int = 20_000, **changes: Any) -> dict[str, Any]:
    return provider(mode).request_within({**REQUEST, **changes}, timeout_ms)


def test_one_prompt_gives_the_neutral_result() -> None:
    result = ask("ok")
    latency = result.pop("latency_ms")
    assert type(latency) is int and latency >= 0
    assert result == {
        "text": "pong",
        "finish_reason": "end_turn",
        "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


def test_unreported_usage_stays_unknown() -> None:
    assert "usage" not in ask("no_usage")


def test_a_named_model_is_selected_or_refused() -> None:
    assert ask("set_model", model="claude-opus-5-5")["text"] == "claude-opus-5-5"
    assert ask("ok", model="claude-sonnet-5-5")["model"] == "claude-sonnet-5-5"
    with pytest.raises(ProviderRejected, match="model_unavailable"):
        ask("ok", model="claude-opus-5-5")


def test_api_billing_never_reaches_the_agent() -> None:
    adapter = provider("env")
    assert adapter.child_env_names == ("HOME", "PATH")
    names = adapter.request(REQUEST)["text"].split(",")
    assert "ANTHROPIC_API_KEY" not in names


def test_permission_and_client_requests_are_refused() -> None:
    assert ask("permission")["text"] == "cancelled:-32601"


@pytest.mark.parametrize(
    ("mode", "error", "code"),
    [
        ("rate_limit", RateLimited, "rate_limited"),
        ("disconnect", ProviderDisconnected, "provider_disconnected"),
        ("auth", ProviderRejected, "authentication_failed"),
        ("agent_error", ProviderRejected, "agent_error"),
    ],
)
def test_failures_are_classified_without_echoing(mode: str, error: Any, code: str) -> None:
    with pytest.raises(error) as raised:
        ask(mode)
    assert str(raised.value) == code


@pytest.mark.parametrize(("mode", "code"), [("garbage", "schema"), ("refusal", "refusal")])
def test_unusable_replies_are_invalid_output(mode: str, code: str) -> None:
    assert ask(mode)["error"] == code


def test_the_deadline_kills_the_agent() -> None:
    started = time.monotonic()
    with pytest.raises(ProviderTimeout):
        ask("hang", timeout_ms=1000)
    assert time.monotonic() - started < 10


def test_oversized_output_is_cut_off() -> None:
    assert ask("flood")["error"] == "schema"


def test_missing_agent_and_command_override() -> None:
    missing = ClaudeAcpProvider(["no-such-acp-agent"], environ=ENVIRON)
    with pytest.raises(ProviderRejected, match="missing_acp_agent"):
        missing.request(REQUEST)
    assert agent_command({}) == ["claude-code-acp"]
    assert agent_command({"UZUMAKI_ACP_COMMAND": "npx -y pkg"}) == ["npx", "-y", "pkg"]
    with pytest.raises(ValueError):
        agent_command({"UZUMAKI_ACP_COMMAND": "'unclosed"})
