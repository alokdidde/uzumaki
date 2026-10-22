"""Scripted local ACP agent for offline adapter tests: JSON-RPC 2.0 over stdio, no network.

Usage: ``python -I fake_acp_agent.py MODE``. Every mode answers ``initialize`` and ``session/new``
and then behaves as named at ``session/prompt`` (rate limit, disconnect, refusal, ...). Ported
from the issue-09 draft.
"""

import json
import os
import sys
import time

CURRENT = "claude-sonnet-5-5"
OTHER = "claude-opus-5-5"
MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
SESSION = "sess-1"


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def update(kind, **fields):
    params = {"sessionId": SESSION, "update": {"sessionUpdate": kind, **fields}}
    send({"jsonrpc": "2.0", "method": "session/update", "params": params})


def chunk(text):
    update("agent_message_chunk", content={"type": "text", "text": text})


def ask_client(request_id, method, params):
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return receive()


def prompt_reply(message_id, prompt, state):
    text = "pong" if "ping" in prompt else "no ping"
    usage = {"inputTokens": 12, "outputTokens": 7}
    stop = "end_turn"
    if MODE == "rate_limit":
        error = {"code": 429, "message": "Rate limit exceeded for sk-ant-SECRET-0000"}
        send({"jsonrpc": "2.0", "id": message_id, "error": error})
        return
    if MODE == "agent_error":
        error = {"code": -32603, "message": "internal failure near sk-ant-SECRET-0000"}
        send({"jsonrpc": "2.0", "id": message_id, "error": error})
        return
    if MODE == "disconnect":
        chunk("po")
        sys.exit(0)
    if MODE == "hang":
        time.sleep(60)
    if MODE == "garbage":
        sys.stdout.write("this is not json-rpc\n")
        sys.stdout.flush()
        return
    if MODE == "flood":
        for _ in range(40):
            chunk("x" * 60000)
    if MODE == "permission":
        permission = ask_client("perm-1", "session/request_permission", {"sessionId": SESSION})
        read = ask_client("fs-1", "fs/read_text_file", {"sessionId": SESSION, "path": "/etc"})
        text = f"{permission['result']['outcome']['outcome']}:{read['error']['code']}"
    if MODE == "env":
        text = ",".join(sorted(os.environ))
    if MODE == "set_model":
        text = state.get("set_model", "none")
    if MODE == "no_usage":
        usage = None
    if MODE == "refusal":
        stop = "refusal"
    update("agent_thought_chunk", content={"type": "text", "text": "thinking"})
    update("tool_call", toolCallId="t1", title="ignored", status="pending")
    chunk(text[:2])
    update("agent_message_chunk", content={"type": "image", "data": "", "mimeType": "image/png"})
    chunk(text[2:])
    result = {"stopReason": stop}
    if usage is not None:
        result["usage"] = usage
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def main():
    state = {}
    while True:
        message = receive()
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
            send({"jsonrpc": "2.0", "id": message_id, "result": result})
        elif method == "session/new":
            if MODE == "auth":
                error = {"code": -32000, "message": "Authentication required"}
                send({"jsonrpc": "2.0", "id": message_id, "error": error})
                continue
            models = {"currentModelId": CURRENT, "availableModels": [{"modelId": CURRENT}]}
            if MODE == "set_model":
                models["availableModels"].append({"modelId": OTHER})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"sessionId": SESSION, "models": models},
                }
            )
        elif method == "session/set_model":
            state["set_model"] = message["params"]["modelId"]
            send({"jsonrpc": "2.0", "id": message_id, "result": {}})
        elif method == "session/prompt":
            prompt_reply(message_id, message["params"]["prompt"][0]["text"], state)
        else:
            error = {"code": -32601, "message": "unexpected method"}
            send({"jsonrpc": "2.0", "id": message_id, "error": error})


if __name__ == "__main__":
    main()
