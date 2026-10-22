"""Live model provider through Claude Code with its Claude sign-in, over the Agent Client Protocol.

``ClaudeAcpProvider`` implements the same provider interface as ``uzumaki.gateway.GatewayProvider``
and takes the same provider-neutral requests. For every request it starts a fresh ACP agent
process (by default ``claude-code-acp``, from the npm package ``@zed-industries/claude-code-acp``,
which uses the Claude Code sign-in: ``claude login``), speaks JSON-RPC 2.0 over its stdio
(``initialize``, ``session/new``, optionally ``session/set_model``, one ``session/prompt``) and
returns the agent's text::

    {"text": "...", "model": "claude-...", "finish_reason": "end_turn", "latency_ms": 2140,
     "usage": {"input_tokens": 950, "output_tokens": 61}}

A new process and session per request means no conversation state survives between calls, so the
planner and generator never share one. ``ACP_DEFAULT_MODEL`` keeps the agent's current model; any
other ``model`` must be one the agent offers, or the call fails (``model_unavailable``); no other
model is substituted.

Subscription only. The agent's environment is an allowlist (``FORWARDED_ENV``) that never passes
API-billing configuration such as ``ANTHROPIC_API_KEY``, and this module never calls ACP
``authenticate``: an agent that is not signed in fails the call (``authentication_failed``)
instead of falling back to another sign-in.

Containment. The agent is the vendor's program, not generated code, and runs with the user's
privileges; this is not a sandbox. Its permission and client-side requests
(``session/request_permission``, ``fs/*``, ``terminal/*``) are refused, it starts in a fresh empty
directory, a watchdog kills its process group at the deadline, and it is always killed and reaped
after the call. Its stderr is discarded and no error echoes prompts, replies or agent messages.

Classification: a missing agent, a sign-in failure, a missing model or another agent error raises
``ProviderRejected``; the deadline raises ``ProviderTimeout``; a rate limit raises ``RateLimited``
and a stream closed early raises ``ProviderDisconnected`` (both retried within budget); a reply
that does not follow the protocol, or a refusal, returns ``{"error": ...}``, which the caller's
validator treats as invalid output. Nothing here runs in the default offline checks: tests point
``command`` at a fake agent.
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
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from uzumaki.codex import flatten_prompt
from uzumaki.faults import ProviderDisconnected, ProviderTimeout, RateLimited
from uzumaki.gateway import MAX_RESPONSE_BYTES, MAX_TIMEOUT_S, ProviderRejected, _check_request

ACP_DEFAULT_MODEL = "claude-default"
DEFAULT_COMMAND = "claude-code-acp"
#: Overrides the agent command line, for example ``npx @zed-industries/claude-code-acp``.
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
STOP_REASONS = ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")
_READ_CHUNK = 65536
_REAP_SECONDS = 5.0
_MAX_SESSION_ID = 256


class _ProtocolError(Exception):
    """The agent did not follow the protocol; becomes ``{"error": "schema"}``."""


def _schema() -> NoReturn:
    raise _ProtocolError


def agent_command(environ: Mapping[str, str] | None = None) -> list[str]:
    """The agent command line: ``UZUMAKI_ACP_COMMAND`` split like a shell would, or the default."""
    source = os.environ if environ is None else environ
    raw = source.get(COMMAND_ENV, "").strip()
    try:
        command = shlex.split(raw) if raw else [DEFAULT_COMMAND]
    except ValueError:
        raise ValueError(f"{COMMAND_ENV} is not a valid command line") from None
    return command


class ClaudeAcpProvider:
    """One live adapter over an ACP agent; see the module docstring for the contract."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        source = os.environ if environ is None else environ
        self.command = list(command) if command is not None else agent_command(source)
        if not self.command or not all(isinstance(p, str) and p for p in self.command):
            raise ValueError("the ACP agent command is empty")
        self._env = {k: source[k] for k in FORWARDED_ENV if isinstance(source.get(k), str)}
        self._clock = clock

    @property
    def child_env_names(self) -> tuple[str, ...]:
        """Names (never values) of the variables the agent process receives."""
        return tuple(sorted(self._env))

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request_within(request, int(MAX_TIMEOUT_S * 1000))

    def request_within(self, request: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        _check_request(request)
        if shutil.which(self.command[0], path=self._env.get("PATH")) is None:
            raise ProviderRejected("missing_acp_agent")
        timeout_s = max(1.0, min(MAX_TIMEOUT_S, timeout_ms / 1000))
        started = self._clock()
        try:
            result = _AcpCall(self.command, self._env, timeout_s).run(request)
        except _ProtocolError:
            result = {"error": "schema"}
        result["latency_ms"] = round((self._clock() - started) * 1000)
        return result


class _AcpCall:
    """One agent process, one session, one prompt."""

    def __init__(self, command: list[str], env: dict[str, str], timeout_s: float) -> None:
        self._command = command
        self._env = env
        self._timeout_s = timeout_s
        self._deadline = time.monotonic() + timeout_s
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
                self._proc = subprocess.Popen(  # noqa: S603
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
                raise ProviderRejected("missing_acp_agent") from None
            watchdog = threading.Timer(self._timeout_s, self._expire)
            watchdog.daemon = True
            watchdog.start()
            try:
                return self._converse(request, workdir)
            except (ProviderDisconnected, _ProtocolError):
                if self._expired.is_set():
                    raise ProviderTimeout("provider_timeout") from None
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
        if init.get("protocolVersion") != ACP_PROTOCOL_VERSION:
            _schema()
        session = self._call("session/new", {"cwd": workdir, "mcpServers": []})
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not 0 < len(session_id) <= _MAX_SESSION_ID:
            _schema()
        self._session = session_id
        model = self._select_model(session, request["model"])
        prompt = [{"type": "text", "text": flatten_prompt(request)}]
        result = self._call("session/prompt", {"sessionId": session_id, "prompt": prompt})
        stop = result.get("stopReason")
        if stop not in STOP_REASONS:
            _schema()
        if stop == "refusal":
            return {"error": "refusal"}
        text = "".join(self._chunks)
        if not text:
            return {"error": "schema"}
        output: dict[str, Any] = {"text": text, "finish_reason": stop}
        if model is not None:
            output["model"] = model
        usage = _usage(result.get("usage"))
        if usage is not None:
            output["usage"] = usage
        return output

    def _select_model(self, session: dict[str, Any], wanted: str) -> str | None:
        """The model that will answer: the agent's current one, or ``wanted`` once selected."""
        models = session.get("models")
        current = models.get("currentModelId") if isinstance(models, dict) else None
        current = current if isinstance(current, str) else None
        if wanted == ACP_DEFAULT_MODEL or wanted == current:
            return current
        available = models.get("availableModels") if isinstance(models, dict) else None
        ids = [m.get("modelId") for m in available or () if isinstance(m, dict)]
        if wanted not in ids:
            raise ProviderRejected("model_unavailable")
        self._call("session/set_model", {"sessionId": self._session, "modelId": wanted})
        return wanted

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
            if result is None:
                return {}
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
        if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
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
            raise ProviderDisconnected("provider_disconnected") from None

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
                raise ProviderTimeout("provider_timeout")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise ProviderTimeout("provider_timeout")
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                raise ProviderDisconnected("provider_disconnected")
            self._read += len(chunk)
            if self._read > MAX_RESPONSE_BYTES:
                _schema()
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
    markers = ("rate limit", "rate_limit", "overloaded", "429", "usage limit")
    if code == 429 or "rate" in kind or any(marker in text for marker in markers):
        raise RateLimited("rate_limited")
    if code == -32000 or "auth" in kind or "authentication" in text or "login" in text:
        raise ProviderRejected("authentication_failed")
    raise ProviderRejected("agent_error")


def _usage(value: Any) -> dict[str, int] | None:
    """ACP usage (``inputTokens``, ``outputTokens``) in neutral names, when both are reported."""
    if not isinstance(value, dict):
        return None
    prompt, completion = value.get("inputTokens"), value.get("outputTokens")
    if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
        return {"input_tokens": prompt, "output_tokens": completion}
    return None
