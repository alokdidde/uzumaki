import json
import socket
from pathlib import Path
from typing import Any

import pytest

from uzumaki.providers import RecordedProvider


def write(tmp_path: Path, data: Any) -> Path:
    p = tmp_path / "cfg.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


def test_replays_recorded_result(fixture_path: Path) -> None:
    provider = RecordedProvider.from_path(fixture_path)
    result = provider.request({"input": "fixture", "role": "planner"})
    assert result == {"decision": "abstain", "reason": "fixture"}
    assert provider.request({"role": "planner", "input": "fixture"}) == result


def test_returns_independent_copies(fixture_path: Path) -> None:
    provider = RecordedProvider.from_path(fixture_path)
    req = {"role": "generator", "input": "fixture"}
    first = provider.request(req)
    first["tags"].append("mutated")
    first["decision"] = "x"
    assert provider.request(req)["tags"] == ["a", "b"]
    assert provider.request(req)["decision"] == "abstain"


def test_unknown_request_fails_without_echoing_input(fixture_path: Path) -> None:
    provider = RecordedProvider.from_path(fixture_path)
    with pytest.raises(ValueError, match="no recorded result") as exc:
        provider.request({"role": "planner", "input": "sk-SECRET-123"})
    assert "SECRET" not in str(exc.value)


def test_non_dict_and_unserializable_requests(fixture_path: Path) -> None:
    provider = RecordedProvider.from_path(fixture_path)
    with pytest.raises(ValueError):
        provider.request(["x"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="JSON-serializable"):
        provider.request({"role": object()})


@pytest.mark.parametrize("path", [None, ""])
def test_missing_configuration(path: Any) -> None:
    with pytest.raises(ValueError, match="missing"):
        RecordedProvider.from_path(path)


def test_file_not_found_hides_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found") as exc:
        RecordedProvider.from_path(tmp_path / "secret-name.json")
    assert "secret-name" not in str(exc.value)


def test_unreadable_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unreadable"):
        RecordedProvider.from_path(tmp_path)


def test_invalid_json_does_not_leak_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid JSON") as exc:
        RecordedProvider.from_path(write(tmp_path, '{"api_key": "sk-SECRET"'))
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"schema_version": "2.0", "records": []},
        {"records": []},
        {"schema_version": "1.0"},
        {"schema_version": "1.0", "records": {}},
        {"schema_version": "1.0", "records": [], "extra": 1},
        {"schema_version": "1.0", "records": ["x"]},
        {"schema_version": "1.0", "records": [{"request": {}, "result": []}]},
        {"schema_version": "1.0", "records": [{"request": {}, "result": {}, "api_key": "sk-S"}]},
    ],
)
def test_malformed_configuration(tmp_path: Path, data: Any) -> None:
    with pytest.raises(ValueError) as exc:
        RecordedProvider.from_path(write(tmp_path, data))
    assert "sk-S" not in str(exc.value)


def test_duplicate_requests_rejected(tmp_path: Path) -> None:
    rec = {"request": {"a": 1}, "result": {"b": 1}}
    with pytest.raises(ValueError, match="duplicate"):
        RecordedProvider.from_path(write(tmp_path, {"schema_version": "1.0", "records": [rec, rec]}))


def test_no_network_during_tests(fixture_path: Path) -> None:
    RecordedProvider.from_path(fixture_path).request({"role": "planner", "input": "fixture"})
    with pytest.raises(AssertionError, match="offline"):
        socket.create_connection(("example.com", 80))
