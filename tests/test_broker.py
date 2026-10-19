"""Versioned tool broker: only declared tools at the declared version reach a host handler."""

import json
from pathlib import Path
from typing import Any

import pytest

from uzumaki.broker import (
    BROKER_PROTOCOL,
    PICK_PLACE_V1_TOOL_VERSION,
    PICK_PLACE_V1_TOOLS,
    ToolBroker,
    ToolSpec,
    pick_place_v1_broker,
)

PROTOCOL = Path(__file__).resolve().parents[1] / "docs" / "protocol" / "task-v1.json"


def echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"echo": args}


def make(**handlers: Any) -> ToolBroker:
    return ToolBroker(
        [ToolSpec(name, "1.0", handler) for name, handler in handlers.items()],
        manifest_version="2.3",
    )


def request(tool: Any = "observe", version: Any = "1.0", args: Any = None, id: Any = 1) -> Any:
    return {"type": "tool", "id": id, "tool": tool, "version": version, "args": args or {}}


def test_declared_tools_match_the_frozen_protocol_profile() -> None:
    profile = json.loads(PROTOCOL.read_text(encoding="utf-8"))["profile"]
    assert tuple(profile["tools"]) == PICK_PLACE_V1_TOOLS


def test_manifest_is_versioned_and_lists_only_names_and_versions() -> None:
    broker = make(observe=echo, pick=echo)
    assert broker.manifest() == {
        "protocol": BROKER_PROTOCOL,
        "manifest_version": "2.3",
        "tools": {"observe": "1.0", "pick": "1.0"},
    }


def test_declared_tool_reaches_its_handler_with_a_copy_of_the_arguments() -> None:
    seen: list[dict[str, Any]] = []

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(args)
        args["mutated"] = True
        return {"ok": 1}

    original = {"x": [1, 2]}
    response, record = make(pick=handler).handle(7, request("pick", args=original), 1024)
    assert response == {"type": "tool_result", "id": 1, "status": "ok", "result": {"ok": 1}}
    assert seen == [{"x": [1, 2], "mutated": True}] and original == {"x": [1, 2]}
    assert (record.seq, record.tool, record.version, record.status) == (7, "pick", "1.0", "ok")


@pytest.mark.parametrize(
    ("req", "reason"),
    [
        (request("write_evaluator"), "unknown_tool"),
        (request("read_heldout"), "unknown_tool"),
        (request("shell"), "unknown_tool"),
        (request("observe", version="2.0"), "version_mismatch"),
        (request("observe", version=""), "version_mismatch"),
        (request("observe", id="1"), "malformed_request"),
        (request("observe", id=True), "malformed_request"),
        (request(tool=["observe"]), "malformed_request"),
        (request(version=1.0), "malformed_request"),
        (
            {"type": "tool", "id": 1, "tool": "observe", "version": "1.0", "args": [1]},
            "malformed_arguments",
        ),
        ({"type": "tool", "id": 1, "tool": "observe", "version": "1.0"}, "malformed_arguments"),
    ],
)
def test_undeclared_or_malformed_requests_are_rejected_without_calling_a_handler(
    req: Any, reason: str
) -> None:
    calls: list[Any] = []
    broker = make(observe=lambda args: calls.append(args) or {})
    response, record = broker.handle(1, req, 1024)
    assert response["status"] == "rejected" and response["reason"] == reason
    assert record.status == "rejected" and record.reason == reason
    assert calls == []


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (lambda args: (_ for _ in ()).throw(RuntimeError("secret-handler-detail")), "tool_failed"),
        (lambda args: [1, 2], "malformed_tool_result"),
        (lambda args: {"v": float("nan")}, "malformed_tool_result"),
        (lambda args: {"v": "x" * 5000}, "oversized_tool_result"),
    ],
)
def test_failing_handlers_are_reported_as_fixed_codes(handler: Any, reason: str) -> None:
    response, record = make(observe=handler).handle(1, request(), 1024)
    assert response == {"type": "tool_result", "id": 1, "status": "failed", "reason": reason}
    assert "secret" not in repr(response) + repr(record)


def test_records_keep_argument_digests_not_raw_content() -> None:
    _, record = make(pick=echo).handle(1, request("pick", args={"token": "sk-secret"}), 1024)
    assert record.args_sha256 is not None and len(record.args_sha256) == 64
    assert "sk-secret" not in repr(record)
    _, record = make().handle(2, request("x" * 500), 1024)
    assert record.tool is None  # oversized untrusted names are not stored


@pytest.mark.parametrize(
    ("tools", "manifest_version"),
    [
        ([ToolSpec("pick", "1.0", echo), ToolSpec("pick", "1.0", echo)], "1.0"),
        ([ToolSpec("Pick", "1.0", echo)], "1.0"),
        ([ToolSpec("../pick", "1.0", echo)], "1.0"),
        ([ToolSpec("pick", "v1", echo)], "1.0"),
        ([ToolSpec("pick", "1.0", "not callable")], "1.0"),  # type: ignore[arg-type]
        (["pick"], "1.0"),
        ([], "latest"),
    ],
)
def test_invalid_manifests_are_refused(tools: Any, manifest_version: str) -> None:
    with pytest.raises(ValueError):
        ToolBroker(tools, manifest_version=manifest_version)


def test_pick_place_broker_refuses_undeclared_tool_handlers() -> None:
    with pytest.raises(ValueError):
        pick_place_v1_broker({"observe": echo, "write_file": echo})
    broker = pick_place_v1_broker({"observe": echo, "abort": echo})
    assert broker.manifest()["tools"] == {
        "abort": PICK_PLACE_V1_TOOL_VERSION,
        "observe": PICK_PLACE_V1_TOOL_VERSION,
    }
