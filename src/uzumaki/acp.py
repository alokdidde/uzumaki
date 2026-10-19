"""Live model adapter: the existing Claude subscription over the Agent Client Protocol (ACP).

``ClaudeAcpProvider`` is the one live adapter (issue 09). For every request it starts a fresh ACP
agent process (for example ``claude-code-acp``, which signs in with the user's Claude
subscription), speaks JSON-RPC 2.0 over the process's stdio (``initialize``, ``session/new``,
model selection, one ``session/prompt``), collects the agent's text and returns a
``uzumaki.model-result/1.0`` dict. A new process and session per request means no conversation
state survives between calls, so the planner and generator never share a session.

Opt-in and billing. The adapter is constructed only with ``opt_in=True``; ``from_environment``
additionally requires ``UZUMAKI_LIVE_PROVIDER=claude-acp`` and ``UZUMAKI_ACP_COMMAND``. The child
environment is an allowlist (``FORWARDED_ENV``) that never forwards API-billing configuration,
and passing such a variable explicitly is refused (``api_billing_refused``). The adapter never
calls ACP ``authenticate``: an agent that needs authentication fails the call
(``auth_required``) instead of falling back to another sign-in method.

Budget. ``LiveBudget`` caps the calls per adapter, the per-call deadline and the protocol bytes
read per call. A call beyond the cap is refused before any process starts. ``BoundedRun``
budgets apply on top.

Model pinning. The requested model ID must be the session's current model or one of its
available models (then selected with ``session/set_model``). An agent that reports no models or
lacks the pinned one fails the call (``model_unavailable``); no other model is substituted.

Containment. The agent is the vendor's program, not generated code, and runs with the host
user's privileges. Its permission and client-side requests (``session/request_permission``,
``fs/*``, ``terminal/*``) are refused, its working directory is a fresh empty temporary
directory, a watchdog kills its process group at the deadline, and it is always killed and reaped
after the call. This is not a sandbox.

Failures map to ``uzumaki.faults``: a rate limit raises ``RateLimited`` and a closed stream
raises ``ProviderDisconnected`` (``BoundedRun`` retries both), the deadline raises
``ProviderTimeout`` and an unexpected message shape raises ``ProviderSchemaError``. Other
refusals raise ``LiveProviderError`` with a fixed reason. No message echoes prompts, replies,
agent error text or environment values; agent stderr is discarded.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from uzumaki.faults import ProviderDisconnected, ProviderSchemaError, ProviderTimeout, RateLimited
from uzumaki.models import RESULT_FORMAT, check_request_shape, render_prompt

PROVIDER_NAME = "claude-acp"
LIVE_ENV_FLAG = "UZUMAKI_LIVE_PROVIDER"
COMMAND_ENV = "UZUMAKI_ACP_COMMAND"
ACP_PROTOCOL_VERSION = 1
#: Environment variables passed to the agent. Everything else, including API keys, is dropped.
#: ``CLAUDE_CODE_OAUTH_TOKEN`` is a subscription sign-in token, not API billing.
FORWARDED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
#: API-billing switches. Never forwarded; refused when passed explicitly (as is any
#: ``ANTHROPIC_*`` variable).
BILLING_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
MAX_LIVE_CALLS = 100
MAX_LIVE_TIMEOUT_MS = 600_000
MAX_LIVE_OUTPUT_BYTES = 4 * 1024 * 1024

_STOP_REASONS = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "cancelled": "cancelled",
}
_USAGE_NAMES = {"inputTokens": "input_tokens", "outputTokens": "output_tokens"}
_READ_CHUNK = 65536
_REAP_SECONDS = 5.0
_MAX_SESSION_ID = 256


class LiveProviderError(Exception):
    """The live adapter refused or could not complete a call. ``reason`` is a fixed code."""

    REASONS = frozenset(
        {
            "live_disabled",
            "invalid_budget",
            "invalid_command",
            "api_billing_refused",
            "live_budget_exhausted",
            "agent_unavailable",
            "auth_required",
            "model_unavailable",
            "agent_error",
            "oversized",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self.REASONS:
            raise AssertionError("unknown live provider reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LiveBudget:
    """Per-adapter live inference budget: calls, per-call deadline and bytes read per call."""

    max_calls: int
    timeout_ms: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.max_calls, MAX_LIVE_CALLS),
            (self.timeout_ms, MAX_LIVE_TIMEOUT_MS),
            (self.max_output_bytes, MAX_LIVE_OUTPUT_BYTES),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise LiveProviderError("invalid_budget")


def _schema() -> NoReturn:
    raise ProviderSchemaError("protocol_error")


def _is_billing(name: str) -> bool:
    return name in BILLING_ENV or name.startswith("ANTHROPIC_")


class ClaudeAcpProvider:
    """Opt-in, budgeted live provider over an ACP agent. One process and session per call."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        budget: LiveBudget,
        opt_in: bool = False,
        environ: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        if opt_in is not True:
            raise LiveProviderError("live_disabled")
        if not isinstance(budget, LiveBudget):
            raise LiveProviderError("invalid_budget")
        if (
            isinstance(command, str)
            or not command
            or not all(isinstance(part, str) and part and "\0" not in part for part in command)
        ):
            raise LiveProviderError("invalid_command")
        extra = dict(extra_env or {})
        for name, value in extra.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise LiveProviderError("invalid_command")
            if _is_billing(name):
                raise LiveProviderError("api_billing_refused")
        source = os.environ if environ is None else environ
        env = {k: source[k] for k in FORWARDED_ENV if isinstance(source.get(k), str)}
        env.update(extra)
        self._command = list(command)
        self._env = env
        self._lock = threading.Lock()
        self.budget = budget
        self.calls = 0

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, budget: LiveBudget
    ) -> ClaudeAcpProvider:
        """Build the adapter only when ``UZUMAKI_LIVE_PROVIDER=claude-acp`` opts in."""
        source = os.environ if environ is None else environ
        if source.get(LIVE_ENV_FLAG) != PROVIDER_NAME:
            raise LiveProviderError("live_disabled")
        try:
            command = shlex.split(source.get(COMMAND_ENV, ""))
        except ValueError:
            raise LiveProviderError("invalid_command") from None
        if not command:
            raise LiveProviderError("agent_unavailable")
        return cls(command, budget=budget, opt_in=True, environ=source)

    @property
    def child_env_names(self) -> tuple[str, ...]:
        """Names (never values) of the variables the agent process receives."""
        return tuple(sorted(self._env))

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, self.budget.timeout_ms)

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        check_request_shape(request)
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise LiveProviderError("invalid_budget")
        with self._lock:
            if self.calls >= self.budget.max_calls:
                raise LiveProviderError("live_budget_exhausted")
            self.calls += 1
        call = _AcpCall(
            self._command,
            self._env,
            min(timeout_ms, self.budget.timeout_ms),
            self.budget.max_output_bytes,
        )
        return call.run(request)


class _AcpCall:
    """One agent process, one session, one prompt."""

    def __init__(
        self, command: list[str], env: dict[str, str], timeout_ms: int, max_bytes: int
    ) -> None:
        self._command = command
        self._env = env
        self._timeout_s = timeout_ms / 1000.0
        self._deadline = time.monotonic() + self._timeout_s
        self._max_bytes = max_bytes
        self._expired = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._read = 0
        self._next_id = 0
        self._session: str | None = None
        self._chunks: list[str] = []

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        workdir = tempfile.mkdtemp(prefix="uzumaki-acp-")
        try:
            try:
                self._proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=self._env,
                    cwd=workdir,
                    start_new_session=True,
                    close_fds=True,
                )
            except (OSError, ValueError):
                raise LiveProviderError("agent_unavailable") from None
            watchdog = threading.Timer(self._timeout_s, self._expire)
            watchdog.daemon = True
            watchdog.start()
            try:
                return self._converse(request, workdir)
            except (ProviderDisconnected, ProviderSchemaError):
                if self._expired.is_set():
                    raise ProviderTimeout("deadline_exceeded") from None
                raise
            finally:
                watchdog.cancel()
        finally:
            self._stop()
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------------ protocol
    def _converse(self, request: dict[str, Any], workdir: str) -> dict[str, Any]:
        init = self._call(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        if type(init.get("protocolVersion")) is not int:
            _schema()
        if init["protocolVersion"] != ACP_PROTOCOL_VERSION:
            raise ProviderSchemaError("unsupported_version")
        session = self._call("session/new", {"cwd": workdir, "mcpServers": []})
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not 0 < len(session_id) <= _MAX_SESSION_ID:
            _schema()
        self._session = session_id
        reported = self._select_model(session, request["model"])
        prompt = [{"type": "text", "text": render_prompt(request)}]
        result = self._call("session/prompt", {"sessionId": session_id, "prompt": prompt})
        stop = result.get("stopReason")
        if not isinstance(stop, str):
            _schema()
        return {
            "format": RESULT_FORMAT,
            "text": "".join(self._chunks),
            "stop_reason": _STOP_REASONS.get(stop, "other"),
            "model": reported,
            "usage": _usage(result.get("usage")),
        }

    def _select_model(self, session: dict[str, Any], pinned: str) -> str:
        models = session.get("models")
        if not isinstance(models, dict):
            raise LiveProviderError("model_unavailable")
        available = models.get("availableModels")
        if not isinstance(available, list):
            _schema()
        if models.get("currentModelId") == pinned:
            return pinned
        ids = [m.get("modelId") for m in available if isinstance(m, dict)]
        if pinned not in ids:
            raise LiveProviderError("model_unavailable")
        self._call("session/set_model", {"sessionId": self._session, "modelId": pinned})
        return pinned

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._message()
            if "method" in message:
                self._handle_agent_message(message)
                continue
            if message.get("id") != request_id:
                _schema()
            if "error" in message:
                _raise_agent_error(message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                _schema()
            return result

    def _handle_agent_message(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if not isinstance(method, str):
            _schema()
        if "id" in message:
            # A request from the agent: refuse permissions and client-side capabilities.
            if method == "session/request_permission":
                reply: dict[str, Any] = {"result": {"outcome": {"outcome": "cancelled"}}}
            else:
                reply = {"error": {"code": -32601, "message": "method not available"}}
            self._send({"jsonrpc": "2.0", "id": message["id"], **reply})
            return
        if method != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != self._session:
            _schema()
        update = params.get("update")
        if not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
            _schema()
        if update["sessionUpdate"] != "agent_message_chunk":
            return
        content = update.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("type"), str):
            _schema()
        if content["type"] == "text":
            text = content.get("text")
            if not isinstance(text, str):
                _schema()
            self._chunks.append(text)

    # ------------------------------------------------------------------------- transport
    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n"
        try:
            self._proc.stdin.write(data.encode("ascii"))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            raise ProviderDisconnected("stream_closed") from None

    def _message(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1 :]
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    _schema()
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    _schema()
                return message
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or self._expired.is_set():
                raise ProviderTimeout("deadline_exceeded")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise ProviderTimeout("deadline_exceeded")
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                raise ProviderDisconnected("stream_closed")
            self._read += len(chunk)
            if self._read > self._max_bytes:
                raise LiveProviderError("oversized")
            self._buffer += chunk

    def _expire(self) -> None:
        self._expired.set()
        self._kill()

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._kill()
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(_REAP_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL did not take effect
            pass


def _raise_agent_error(error: Any) -> NoReturn:
    """Classify a JSON-RPC error without keeping or echoing its message."""
    if not isinstance(error, dict):
        _schema()
    code = error.get("code")
    message = error.get("message")
    text = message.lower() if isinstance(message, str) else ""
    data = error.get("data")
    kind = ""
    if isinstance(data, dict):
        kind = " ".join(str(data.get(k, "")) for k in ("type", "errorType", "code")).lower()
    markers = ("rate limit", "rate_limit", "overloaded", "429")
    if code == 429 or "rate" in kind or any(marker in text for marker in markers):
        raise RateLimited("rate_limited")
    if code == -32000 or "auth" in kind or "authentication" in text:
        raise LiveProviderError("auth_required")
    raise LiveProviderError("agent_error")


def _usage(value: Any) -> dict[str, Any] | None:
    """Map ACP usage names to neutral ones; unknown fields keep their name only, not values."""
    if value is None:
        return None
    if not isinstance(value, dict):
        _schema()
    usage: dict[str, Any] = {}
    for name in value:
        if name in _USAGE_NAMES:
            usage[_USAGE_NAMES[name]] = value[name]
        elif isinstance(name, str) and name not in usage:
            usage[name] = None
    return usage
