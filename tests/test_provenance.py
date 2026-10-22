"""Per-call ``model_provenance`` records (``uzumaki.provenance``), checked against contract v1.0."""

import argparse
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from uzumaki import backends
from uzumaki.baseline import MODEL_BUDGETS
from uzumaki.evidence import sha256_of
from uzumaki.faults import ProviderTimeout, RateLimited
from uzumaki.gateway import ProviderRejected
from uzumaki.provenance import LOG_FORMAT, ProvenanceLog, error_code
from uzumaki.supervisor import BoundedRun

CONTRACT = Path(__file__).resolve().parents[1] / "docs/contracts/v1.json"
SCHEMA = json.loads(CONTRACT.read_text(encoding="utf-8"))["records"]["model_provenance"]
SECRET = "sk-SECRET-0000"
REQUEST = {
    "role": "planner",
    "model": "m-1",
    "prompt_version": "planner-v2",
    "system": "system prompt " + SECRET,
    "messages": [{"role": "user", "content": "context " + SECRET}],
    "max_tokens": 50,
}


class Scripted:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[no-any-return]


def log(*answers: Any, **kwargs: Any) -> ProvenanceLog:
    ticks = iter(range(1000, 100000, 10))
    return ProvenanceLog(
        Scripted(*answers),
        "codex",
        clock=lambda: 1_700_000_000_000,
        timer=lambda: next(ticks),
        **kwargs,
    )


def payloads(provenance: ProvenanceLog) -> list[dict[str, Any]]:
    for call in provenance.calls:
        jsonschema.Draft202012Validator(SCHEMA).validate(call["record"])
    return [call["record"]["payload"] for call in provenance.calls]


def test_a_successful_call_is_one_contract_record() -> None:
    answer = {
        "text": "ok " + SECRET,
        "model": "gpt-x",
        "latency_ms": 812,
        "usage": {"input_tokens": 9, "output_tokens": 2},
    }
    provenance = log(answer)
    assert provenance.request(REQUEST) == answer
    [payload] = payloads(provenance)
    assert payload == {
        "provenance_id": "planner-0001",
        "provider": "codex",
        "model": "m-1",
        "reported_model": "gpt-x",
        "role": "planner",
        "replay_mode": "live",
        "prompt_sha256": sha256_of({"prompt_version": "planner-v2", "system": REQUEST["system"]}),
        "input_sha256": sha256_of(REQUEST["messages"]),
        "output_sha256": sha256_of(answer),
        "requested_at_ms": 1_700_000_000_000,
        "latency_ms": 812,
        "usage": {"status": "reported", "input_tokens": 9, "output_tokens": 2, "cost_usd": None},
    }
    assert provenance.calls[0]["error"] is None
    assert SECRET not in json.dumps(provenance.document())


def test_failed_and_invalid_calls_are_recorded_with_a_fixed_code() -> None:
    provenance = log(
        RateLimited("rate_limited"),
        ProviderRejected("authentication_failed"),
        {"error": "schema"},
        {"text": "x", "usage": {"input_tokens": 3}},
    )
    with pytest.raises(RateLimited):
        provenance.request(REQUEST)
    with pytest.raises(ProviderRejected):
        provenance.request(REQUEST)
    provenance.request(REQUEST)
    provenance.request({**REQUEST, "role": "generator"})
    first, second, third, fourth = payloads(provenance)
    assert first["output_sha256"] is None and second["output_sha256"] is None
    assert first["usage"]["status"] == "unknown" and first["latency_ms"] == 10
    assert fourth["usage"] == {
        "status": "partially_reported",
        "input_tokens": 3,
        "output_tokens": None,
        "cost_usd": None,
    }
    assert fourth["provenance_id"] == "generator-0004" and fourth["reported_model"] is None
    errors = [call["error"] for call in provenance.calls]
    assert errors == ["rate_limited", "authentication_failed", "schema", None]


def test_error_codes_never_echo_messages() -> None:
    assert error_code(ProviderTimeout(SECRET)) == "provider_timeout"
    assert error_code(RuntimeError(SECRET)) == "provider_error"


def test_each_retry_in_a_bounded_run_is_its_own_record() -> None:
    provenance = log(ProviderTimeout("provider_timeout"), {"text": "ok", "latency_ms": 5})
    run = BoundedRun(run_id="r1", budgets=MODEL_BUDGETS, provider=provenance)
    run.start()
    assert run.call_model("planner", REQUEST)["text"] == "ok"
    assert [call["error"] for call in provenance.calls] == ["provider_timeout", None]
    assert [p["provenance_id"] for p in payloads(provenance)] == ["planner-0001", "planner-0002"]


def test_model_commands_write_the_log(tmp_path: Path) -> None:
    provenance = log({"text": "ok"}, replay_mode="recorded")
    provenance.request(REQUEST)
    options = argparse.Namespace(record=None, out=tmp_path)
    backends.save_exchanges(options, provenance)
    document = json.loads((tmp_path / "model-provenance.json").read_text(encoding="utf-8"))
    assert document["format"] == LOG_FORMAT
    assert document["calls"][0]["record"]["payload"]["replay_mode"] == "recorded"


def test_names_are_checked() -> None:
    with pytest.raises(ValueError):
        ProvenanceLog(Scripted(), "")
    with pytest.raises(ValueError):
        ProvenanceLog(Scripted(), "codex", replay_mode="dry")
