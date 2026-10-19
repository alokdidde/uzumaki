"""Scripted local ACP agent for offline adapter tests: JSON-RPC 2.0 over stdio, no network.

Usage: ``python -I fake_acp_agent.py MODE``. Every mode answers ``initialize`` and ``session/new``
and then behaves as named at ``session/prompt`` (rate limit, disconnect, schema change, ...).
"""

import json
import os
import sys
import time

PINNED = "claude-sonnet-5-5"
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


def answer(prompt, rationale):
    if "planner role" in prompt:
        reply = {
            "decision": "abstain",
            "skill_id": None,
            "skill_version": None,
            "parameters": {},
            "rationale": rationale,
            "abstain_reason": "fixture agent",
        }
    else:
        reply = {"format": "constrained_program_text", "content": "observe()", "notes": rationale}
    return json.dumps(reply)


def models():
    if MODE == "set_model":
        return {
            "currentModelId": "default",
            "availableModels": [{"modelId": "default"}, {"modelId": PINNED}],
        }
    if MODE == "wrong_model":
        return {"currentModelId": "default", "availableModels": [{"modelId": "default"}]}
    return {"currentModelId": PINNED, "availableModels": [{"modelId": PINNED}]}


def ask_client(request_id, method, params):
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return receive()


def prompt_reply(message_id, prompt, state):
    rationale = "fixture"
    usage = {"inputTokens": 12, "outputTokens": 7}
    stop = "end_turn"
    if MODE == "rate_limit":
        error = {"code": 429, "message": "Rate limit exceeded for sk-ant-SECRET-0000"}
        error["data"] = {"type": "rate_limit_error"}
        send({"jsonrpc": "2.0", "id": message_id, "error": error})
        return
    if MODE == "agent_error":
        error = {"code": -32603, "message": "internal failure near sk-ant-SECRET-0000"}
        send({"jsonrpc": "2.0", "id": message_id, "error": error})
        return
    if MODE == "disconnect":
        chunk('{"decision": ')
        sys.exit(0)
    if MODE == "hang":
        time.sleep(60)
    if MODE == "garbage":
        sys.stdout.write("this is not json-rpc\n")
        sys.stdout.flush()
        return
    if MODE == "chunk_schema":
        update("agent_message_chunk", content="plain text instead of a content block")
        return
    if MODE == "flood":
        for _ in range(200):
            chunk("x" * 60000)
        return
    if MODE == "schema_change":
        send({"jsonrpc": "2.0", "id": message_id, "result": {"stop": "end_turn"}})
        return
    if MODE == "permission":
        permission = ask_client("perm-1", "session/request_permission", {"sessionId": SESSION})
        read = ask_client("fs-1", "fs/read_text_file", {"sessionId": SESSION, "path": "/etc"})
        outcome = permission["result"]["outcome"]["outcome"]
        code = read["error"]["code"]
        rationale = f"{outcome}:{code}"
    if MODE == "env":
        rationale = ",".join(sorted(os.environ))
    if MODE == "set_model":
        rationale = "set_model_seen" if state.get("set_model") == PINNED else "set_model_missing"
    if MODE == "unknown_usage":
        usage.update({"cachedReadTokens": 3, "thoughtTokens": {"n": 1}, "sk-SECRET-name": 1})
    if MODE == "no_usage":
        usage = None
    if MODE == "stop_max":
        stop = "max_tokens"
    if MODE == "unknown_stop":
        stop = "paused_for_review"
    update("agent_thought_chunk", content={"type": "text", "text": "thinking"})
    update("tool_call", toolCallId="t1", title="ignored", status="pending")
    text = answer(prompt, rationale)
    chunk(text[:10])
    update("agent_message_chunk", content={"type": "image", "data": "", "mimeType": "image/png"})
    chunk(text[10:])
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
            version = 2 if MODE == "acp_v2" else 1
            result = {"protocolVersion": version, "agentCapabilities": {}, "authMethods": []}
            send({"jsonrpc": "2.0", "id": message_id, "result": result})
        elif method == "session/new":
            if MODE == "auth":
                error = {"code": -32000, "message": "Authentication required"}
                send({"jsonrpc": "2.0", "id": message_id, "error": error})
                continue
            result = {"sessionId": SESSION}
            if MODE != "no_models":
                result["models"] = models()
            send({"jsonrpc": "2.0", "id": message_id, "result": result})
        elif method == "session/set_model":
            state["set_model"] = message["params"]["modelId"]
            send({"jsonrpc": "2.0", "id": message_id, "result": {}})
        elif method == "session/prompt":
            prompt = message["params"]["prompt"][0]["text"]
            prompt_reply(message_id, prompt, state)
        else:
            error = {"code": -32601, "message": "unexpected method"}
            send({"jsonrpc": "2.0", "id": message_id, "error": error})


if __name__ == "__main__":
    main()
