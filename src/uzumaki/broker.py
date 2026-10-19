"""Versioned host-side tool broker for restricted workers.

A candidate in a restricted worker has no direct access to tools, files or the network. It sends
tool requests over a pipe, and the host answers them through a ``ToolBroker``. The broker allows
only tools declared in its manifest, with the declared version, and runs their handlers in the
host. Handlers are trusted host adapter code (for example a simulator adapter), never candidate
code. Everything else is rejected with a fixed reason and recorded by digest, never by raw
content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BROKER_PROTOCOL = "uzumaki.broker/1.0"

#: Tools declared by ``pick-place-v1`` (``profile.tools`` in docs/protocol/task-v1.json).
PICK_PLACE_V1_TOOLS = ("observe", "pick", "place", "abort")
PICK_PLACE_V1_TOOL_VERSION = "1.0"

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}")

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """One declared tool: name, version and the host handler that implements it."""

    name: str
    version: str
    handler: Handler


@dataclass(frozen=True)
class ToolCallRecord:
    """Audit record of one broker request. Arguments are kept as a SHA-256 digest only."""

    seq: int
    tool: str | None
    version: str | None
    status: str  # "ok", "rejected" or "failed"
    reason: str | None
    args_sha256: str | None


def _digest(value: Any) -> str | None:
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _short(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 else None


class ToolBroker:
    """Dispatch declared, versioned tools. Rejections are answers, not exceptions."""

    def __init__(self, tools: Iterable[ToolSpec], *, manifest_version: str) -> None:
        if not isinstance(manifest_version, str) or not _VERSION.fullmatch(manifest_version):
            raise ValueError("manifest_version must look like '1.0'")
        specs: dict[str, ToolSpec] = {}
        for spec in tools:
            if not isinstance(spec, ToolSpec):
                raise ValueError("tools must be ToolSpec instances")
            if not _NAME.fullmatch(spec.name):
                raise ValueError("tool names must match [a-z][a-z0-9_]{0,63}")
            if not _VERSION.fullmatch(spec.version):
                raise ValueError("tool versions must look like '1.0'")
            if not callable(spec.handler):
                raise ValueError("tool handlers must be callable")
            if spec.name in specs:
                raise ValueError("duplicate tool name")
            specs[spec.name] = spec
        self.manifest_version = manifest_version
        self._specs = specs

    def manifest(self) -> dict[str, Any]:
        """The versioned manifest the worker is told about (names and versions only)."""
        return {
            "protocol": BROKER_PROTOCOL,
            "manifest_version": self.manifest_version,
            "tools": {name: spec.version for name, spec in sorted(self._specs.items())},
        }

    def handle(
        self, seq: int, request: Mapping[str, Any], max_result_bytes: int
    ) -> tuple[dict[str, Any], ToolCallRecord]:
        """Answer one ``{"type": "tool", ...}`` request from a worker.

        Returns the response to send back and the audit record. Never raises for bad requests:
        an unknown tool, a version mismatch or malformed arguments are rejected, and a failing
        handler or unusable handler result is reported as ``failed``.
        """
        call_id = request.get("id")
        tool = request.get("tool")
        version = request.get("version")
        args = request.get("args")
        digest = _digest(args)

        def answer(
            status: str, reason: str | None, result: Any = None
        ) -> tuple[dict[str, Any], ToolCallRecord]:
            response: dict[str, Any] = {"type": "tool_result", "id": call_id, "status": status}
            if reason is not None:
                response["reason"] = reason
            if status == "ok":
                response["result"] = result
            record = ToolCallRecord(seq, _short(tool), _short(version), status, reason, digest)
            return response, record

        if type(call_id) is not int or not isinstance(tool, str) or not isinstance(version, str):
            call_id = call_id if type(call_id) is int else None
            return answer("rejected", "malformed_request")
        spec = self._specs.get(tool)
        if spec is None:
            return answer("rejected", "unknown_tool")
        if version != spec.version:
            return answer("rejected", "version_mismatch")
        if not isinstance(args, dict) or digest is None:
            return answer("rejected", "malformed_arguments")
        try:
            result = spec.handler(json.loads(json.dumps(args)))
        except Exception:
            return answer("failed", "tool_failed")
        if not isinstance(result, dict):
            return answer("failed", "malformed_tool_result")
        try:
            size = len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
        except (TypeError, ValueError, RecursionError):
            return answer("failed", "malformed_tool_result")
        if size > max_result_bytes:
            return answer("failed", "oversized_tool_result")
        return answer("ok", None, result)


def pick_place_v1_broker(handlers: Mapping[str, Handler]) -> ToolBroker:
    """Broker for the ``pick-place-v1`` tools. Undeclared tool names are refused."""
    unknown = set(handlers) - set(PICK_PLACE_V1_TOOLS)
    if unknown:
        raise ValueError("handlers for undeclared tools")
    return ToolBroker(
        (
            ToolSpec(name, PICK_PLACE_V1_TOOL_VERSION, handlers[name])
            for name in PICK_PLACE_V1_TOOLS
            if name in handlers
        ),
        manifest_version="1.0",
    )
