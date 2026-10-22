"""Model backends: one registry, so the planner and generator run on any supported model.

The harness only depends on the provider interface (``request(dict) -> dict``, optionally
``request_within``) and provider-neutral requests (see ``uzumaki.gateway``). Which model answers
is configuration:

- ``--provider NAME`` (or ``UZUMAKI_PROVIDER``) picks a backend from ``BACKENDS``;
- ``--model ID`` (or ``UZUMAKI_MODEL``) overrides its default model;
- ``--base-url URL`` / ``--api-key-env VAR`` (or ``UZUMAKI_BASE_URL`` / ``UZUMAKI_API_KEY_ENV``)
  point a backend at another endpoint, for example any OpenAI-compatible server with
  ``--provider openai-compatible``.

Backend kinds: ``openai`` (OpenAI-compatible chat completions: ``GatewayProvider``), ``anthropic``
(the Messages API: ``AnthropicProvider``), ``codex`` (the Codex CLI with its own sign-in:
``CodexProvider``) and ``acp`` (Claude Code with its Claude sign-in, over the Agent Client Protocol:
``ClaudeAcpProvider``). Adding a backend is one ``BACKENDS`` entry, or a new kind with an adapter
that implements the same interface. ``--record`` keeps exchanges and ``--replay`` answers from them
with no model at all. Every model call, live or replayed, is also logged as a ``model_provenance``
record (``uzumaki.provenance``) in ``<out>/model-provenance.json``. Keys are read from the
environment only, never from flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uzumaki.anthropic_api import ANTHROPIC_DEFAULT_MODEL, AnthropicProvider
from uzumaki.claude_acp import ACP_DEFAULT_MODEL, ClaudeAcpProvider
from uzumaki.codex import CODEX_DEFAULT_MODEL, CodexProvider
from uzumaki.gateway import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    GatewayProvider,
    RecordingProvider,
)
from uzumaki.provenance import ProvenanceLog
from uzumaki.providers import RecordedProvider

KINDS = ("openai", "anthropic", "codex", "acp")


@dataclass(frozen=True)
class Backend:
    """A named model backend. ``key_env`` None means no key (local servers or a CLI sign-in)."""

    name: str
    kind: str
    base_url: str | None
    key_env: str | None
    default_model: str | None
    about: str
    #: OpenAI-style reasoning models: no temperature, ``max_completion_tokens``.
    reasoning: bool = False


BACKENDS: dict[str, Backend] = {
    b.name: b
    for b in (
        Backend(
            "vercel",
            "openai",
            DEFAULT_BASE_URL,
            DEFAULT_KEY_ENV,
            DEFAULT_MODEL,
            "Vercel AI Gateway (many vendors' models behind one key)",
        ),
        Backend(
            "anthropic",
            "anthropic",
            "https://api.anthropic.com",
            "ANTHROPIC_API_KEY",
            ANTHROPIC_DEFAULT_MODEL,
            "Anthropic Messages API (Claude)",
        ),
        Backend(
            "openai",
            "openai",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            None,
            "OpenAI API",
            reasoning=True,
        ),
        Backend(
            "openrouter",
            "openai",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            None,
            "OpenRouter (many vendors' models behind one key)",
        ),
        Backend(
            "ollama",
            "openai",
            "http://localhost:11434/v1",
            None,
            None,
            "a local Ollama server (no key)",
        ),
        Backend(
            "openai-compatible",
            "openai",
            None,
            None,
            None,
            "any OpenAI-compatible endpoint: needs --base-url, optional --api-key-env",
        ),
        Backend(
            "codex",
            "codex",
            None,
            None,
            CODEX_DEFAULT_MODEL,
            "the Codex CLI with its ChatGPT sign-in (codex login)",
        ),
        Backend(
            "claude",
            "acp",
            None,
            None,
            ACP_DEFAULT_MODEL,
            "Claude Code with its Claude sign-in (claude login), via claude-code-acp",
        ),
    )
}
#: Older names kept working.
ALIASES = {"gateway": "vercel"}
DEFAULT_BACKEND = "vercel"


class BackendError(ValueError):
    """The requested backend cannot be used as configured. The message says what is missing."""


def resolve(name: str) -> Backend:
    backend = BACKENDS.get(ALIASES.get(name, name))
    if backend is None:
        raise BackendError(f"unknown provider {name!r}; known: {', '.join(sorted(BACKENDS))}")
    return backend


def build_provider(
    name: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
) -> tuple[Any, str]:
    """A live provider and the model ID to request from it."""
    backend = resolve(name)
    chosen = model or backend.default_model
    if not chosen:
        raise BackendError(f"provider {backend.name!r} needs --model (or UZUMAKI_MODEL)")
    url = base_url or backend.base_url
    key_env = api_key_env or backend.key_env
    if key_env is not None and not os.environ.get(key_env):
        raise BackendError(f"provider {backend.name!r} needs {key_env} in the environment")
    if backend.kind == "codex":
        return CodexProvider(), chosen
    if backend.kind == "acp":
        return ClaudeAcpProvider(), chosen
    if url is None:
        raise BackendError(f"provider {backend.name!r} needs --base-url (or UZUMAKI_BASE_URL)")
    if backend.kind == "anthropic":
        assert key_env is not None
        return AnthropicProvider(base_url=url, key_env=key_env), chosen
    return GatewayProvider(base_url=url, key_env=key_env, reasoning=backend.reasoning), chosen


# ---------------------------------------------------------------------- command lines
def add_model_options(parser: argparse.ArgumentParser) -> None:
    """``--provider``, ``--model``, ``--base-url``, ``--api-key-env`` and ``--record``/``--replay``,
    shared by every command that calls a model."""
    names = ", ".join(f"{b.name} ({b.about})" for b in BACKENDS.values())
    parser.add_argument(
        "--provider",
        default=os.environ.get("UZUMAKI_PROVIDER", DEFAULT_BACKEND),
        help=f"model backend: {names}",
    )
    parser.add_argument("--model", default=os.environ.get("UZUMAKI_MODEL") or None)
    parser.add_argument("--base-url", default=os.environ.get("UZUMAKI_BASE_URL") or None)
    parser.add_argument(
        "--api-key-env",
        default=os.environ.get("UZUMAKI_API_KEY_ENV") or None,
        help="name of the environment variable holding the key (never the key itself)",
    )
    exchanges = parser.add_mutually_exclusive_group()
    exchanges.add_argument("--record", type=Path, help="keep model exchanges in this file")
    exchanges.add_argument("--replay", type=Path, help="answer from recorded model exchanges")


def model_provider(options: argparse.Namespace) -> Any:
    """The provider the options ask for, or ``None`` after printing why it is unavailable.
    Sets ``options.model`` to the model actually requested."""
    if options.replay is not None:
        if options.model is None:
            try:
                options.model = resolve(options.provider).default_model
            except BackendError:
                options.model = None
        if options.model is None:
            print("--replay needs the --model the exchanges were recorded with", file=sys.stderr)
            return None
        return ProvenanceLog(
            RecordedProvider.from_path(options.replay), options.provider, replay_mode="recorded"
        )
    try:
        provider, options.model = build_provider(
            options.provider,
            model=options.model,
            base_url=options.base_url,
            api_key_env=options.api_key_env,
        )
    except (BackendError, ValueError) as error:
        print(error, file=sys.stderr)
        return None
    if options.record is not None:
        provider = RecordingProvider(provider)
    return ProvenanceLog(provider, options.provider)


def save_exchanges(options: argparse.Namespace, provider: Any) -> None:
    """Write ``--record`` exchanges and ``<out>/model-provenance.json`` (when ``--out`` exists)."""
    log = provider if isinstance(provider, ProvenanceLog) else None
    inner = log.inner if log is not None else provider
    if isinstance(inner, RecordingProvider) and options.record is not None:
        options.record.write_text(json.dumps(inner.document(), indent=1) + "\n")
    out = getattr(options, "out", None)
    if log is not None and isinstance(out, Path) and out.is_dir():
        (out / "model-provenance.json").write_text(json.dumps(log.document(), indent=1) + "\n")
