"""Live model provider through OpenAI's Codex CLI, signed in with a ChatGPT account.

``CodexProvider`` implements the same provider interface as ``uzumaki.gateway.GatewayProvider``
and takes the same provider-neutral requests. Each request runs one ``codex exec`` turn in an
empty, read-only, ephemeral workspace; the system prompt and messages are sent on stdin as one
prompt, and the reply is the turn's last agent message. Results are::

    {"text": "...", "model": "gpt-...", "finish_reason": "stop", "latency_ms": 2140,
     "usage": {"input_tokens": 950, "output_tokens": 61}}

Authentication stays with the Codex CLI (``codex login`` or ``codex login --device-auth``): this
module never reads, copies or logs its tokens. ``CODEX_DEFAULT_MODEL`` lets the CLI choose its own
model; any other ``model`` is passed as ``--model``. Classification: a missing CLI or a sign-in
failure raises ``ProviderRejected``; a timeout raises ``ProviderTimeout``; rate limits and other
failed turns raise ``TransientProviderError``; output without a final message returns
``{"error": "schema"}``. Nothing here runs in the default offline checks: tests inject ``runner``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import Any

from uzumaki.faults import ProviderTimeout, TransientProviderError
from uzumaki.gateway import MAX_RESPONSE_BYTES, MAX_TIMEOUT_S, ProviderRejected, _check_request

CODEX_DEFAULT_MODEL = "codex-default"
AUTH_MARKERS = ("401", "unauthorized", "not logged in", "login", "sign in", "refresh token")
RATE_MARKERS = ("429", "rate limit", "usage limit", "too many requests")

#: ``runner(argv, stdin, cwd, timeout_s) -> (exit_code, stdout_bytes, stderr_bytes)``.
Runner = Callable[[Sequence[str], bytes, str, float], tuple[int, bytes, bytes]]


def subprocess_runner(
    argv: Sequence[str], stdin: bytes, cwd: str, timeout_s: float
) -> tuple[int, bytes, bytes]:
    """Run the CLI; ``subprocess.TimeoutExpired`` becomes ``TimeoutError``."""
    try:
        done = subprocess.run(  # noqa: S603
            list(argv), input=stdin, cwd=cwd, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    return done.returncode, done.stdout, done.stderr


def _prompt(request: dict[str, Any]) -> str:
    """Flatten the system prompt and conversation into one plain-text prompt."""
    parts = [
        "You are answering as a model endpoint, not as a coding agent. Do not run commands or "
        "edit files. Follow the instructions below and reply with the answer only.",
        "<system>\n" + request["system"] + "\n</system>",
    ]
    for message in request["messages"]:
        parts.append(f"<{message['role']}>\n{message['content']}\n</{message['role']}>")
    return "\n\n".join(parts) + "\n"


def _classify_failure(text: str) -> Exception:
    lowered = text.lower()
    if any(marker in lowered for marker in RATE_MARKERS):
        return TransientProviderError("provider_unavailable")
    if any(marker in lowered for marker in AUTH_MARKERS):
        return ProviderRejected("authentication_failed")
    return TransientProviderError("provider_unavailable")


def _events(stdout: bytes) -> list[dict[str, Any]]:
    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("input_tokens"), raw.get("output_tokens")
    if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
        return {"input_tokens": prompt, "output_tokens": completion}
    return None


class CodexProvider:
    """One live adapter over ``codex exec``; see the module docstring for the contract."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executable = executable
        self._runner = runner or subprocess_runner
        self._find_cli = runner is None
        self._clock = clock

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, int(MAX_TIMEOUT_S * 1000))

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        _check_request(request)
        if self._find_cli and shutil.which(self.executable) is None:
            raise ProviderRejected("missing_codex_cli")
        argv = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--color",
            "never",
        ]
        if request["model"] != CODEX_DEFAULT_MODEL:
            argv += ["--model", request["model"]]
        argv.append("-")
        timeout_s = max(1.0, min(MAX_TIMEOUT_S, timeout_ms / 1000))
        started = self._clock()
        with tempfile.TemporaryDirectory(prefix="uzumaki-codex-") as workspace:
            try:
                code, stdout, stderr = self._runner(
                    argv, _prompt(request).encode("utf-8"), workspace, timeout_s
                )
            except TimeoutError:
                raise ProviderTimeout("provider_timeout") from None
            except FileNotFoundError:
                raise ProviderRejected("missing_codex_cli") from None
            except OSError:
                raise TransientProviderError("provider_unreachable") from None
        latency_ms = round((self._clock() - started) * 1000)
        if len(stdout) > MAX_RESPONSE_BYTES:
            return {"error": "oversized", "latency_ms": latency_ms}
        events = _events(stdout)
        text: str | None = None
        usage: dict[str, int] | None = None
        model: str | None = None
        failure: str | None = None
        for event in events:
            kind = event.get("type")
            item = event.get("item")
            if kind == "item.completed" and isinstance(item, dict):
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
            elif kind == "turn.completed":
                usage = _usage(event.get("usage"))
            elif kind in ("turn.failed", "error"):
                failure = json.dumps(event)
            elif kind == "thread.started" and isinstance(event.get("model"), str):
                model = event["model"]
        if failure is not None or (code != 0 and text is None):
            raise _classify_failure((failure or "") + stderr.decode("utf-8", errors="replace"))
        if text is None:
            return {"error": "schema", "latency_ms": latency_ms}
        result: dict[str, Any] = {
            "text": text,
            "model": model or request["model"],
            "finish_reason": "stop",
            "latency_ms": latency_ms,
        }
        if usage is not None:
            result["usage"] = usage
        return result
