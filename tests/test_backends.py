"""The model backend registry (``uzumaki.backends``): selection, defaults and clear failures."""

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from uzumaki import backends
from uzumaki.anthropic_api import AnthropicProvider
from uzumaki.backends import BACKENDS, BackendError, build_provider, resolve
from uzumaki.codex import CodexProvider
from uzumaki.gateway import GatewayProvider, RecordingProvider
from uzumaki.providers import RecordedProvider

KEYS = ("AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*KEYS, "UZUMAKI_PROVIDER", "UZUMAKI_MODEL", "UZUMAKI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("UZUMAKI_API_KEY_ENV", raising=False)


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    backends.add_model_options(parser)
    return parser.parse_args(list(argv))


@pytest.mark.parametrize(
    ("name", "kind", "url"),
    [
        ("vercel", GatewayProvider, "https://ai-gateway.vercel.sh/v1/chat/completions"),
        ("gateway", GatewayProvider, "https://ai-gateway.vercel.sh/v1/chat/completions"),
        ("anthropic", AnthropicProvider, "https://api.anthropic.com/v1/messages"),
        ("openai", GatewayProvider, "https://api.openai.com/v1/chat/completions"),
        ("openrouter", GatewayProvider, "https://openrouter.ai/api/v1/chat/completions"),
    ],
)
def test_hosted_backends(name: str, kind: type, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.setenv(key, "not-a-key")
    provider, model = build_provider(name, model="some-model")
    assert isinstance(provider, kind) and provider.url == url and model == "some-model"


def test_only_openai_uses_the_reasoning_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert build_provider("openai", model="m")[0].reasoning
    assert not build_provider("openrouter", model="m")[0].reasoning


def test_default_models() -> None:
    assert resolve("vercel").default_model == "anthropic/claude-sonnet-5.5"
    assert resolve("anthropic").default_model == "claude-opus-5-5"
    assert isinstance(build_provider("codex")[0], CodexProvider)


def test_missing_model_key_or_url_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BackendError, match="unknown provider 'nope'"):
        resolve("nope")
    with pytest.raises(BackendError, match="needs AI_GATEWAY_API_KEY"):
        build_provider("vercel")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(BackendError, match="needs --model"):
        build_provider("openai")
    with pytest.raises(BackendError, match="needs --base-url"):
        build_provider("openai-compatible", model="m")


def test_local_and_custom_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = build_provider("ollama", model="llama3.3")
    assert provider.url == "http://localhost:11434/v1/chat/completions" and provider.key_env is None
    provider, _ = build_provider(
        "openai-compatible", model="m", base_url="http://127.0.0.1:8000/v1"
    )
    assert provider.key_env is None
    with pytest.raises(BackendError, match="needs MY_KEY"):
        build_provider(
            "openai-compatible", model="m", base_url="https://x.test/v1", api_key_env="MY_KEY"
        )
    monkeypatch.setenv("MY_KEY", "k")
    provider, _ = build_provider(
        "openai-compatible", model="m", base_url="https://x.test/v1", api_key_env="MY_KEY"
    )
    assert provider.key_env == "MY_KEY"
    with pytest.raises(ValueError):
        build_provider("openai-compatible", model="m", base_url="http://x.test/v1")


def test_every_backend_names_a_known_kind() -> None:
    assert {b.kind for b in BACKENDS.values()} <= set(backends.KINDS)


def test_command_line_and_environment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    options = parse()
    assert options.provider == "vercel" and options.model is None
    monkeypatch.setenv("UZUMAKI_PROVIDER", "ollama")
    monkeypatch.setenv("UZUMAKI_MODEL", "qwen3")
    options = parse()
    assert (options.provider, options.model) == ("ollama", "qwen3")
    provider = backends.model_provider(options)
    assert isinstance(provider, GatewayProvider) and options.model == "qwen3"
    options = parse("--provider", "ollama", "--model", "m", "--record", "x.json")
    assert isinstance(backends.model_provider(options), RecordingProvider)


def test_unavailable_backend_prints_why(capsys: pytest.CaptureFixture[str]) -> None:
    assert backends.model_provider(parse("--provider", "anthropic")) is None
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_replay_needs_no_backend(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "exchanges.json"
    path.write_text(json.dumps(RecordingProvider(_Echo()).document()), encoding="utf-8")
    options = parse("--replay", str(path))
    assert isinstance(backends.model_provider(options), RecordedProvider)
    assert options.model == "anthropic/claude-sonnet-5.5"
    options = parse("--provider", "openai", "--replay", str(path))
    assert backends.model_provider(options) is None
    assert "--replay needs the --model" in capsys.readouterr().err


class _Echo:
    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"text": request["system"]}
