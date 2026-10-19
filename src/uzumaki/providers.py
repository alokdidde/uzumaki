"""Recorded (fake) model provider. Replays versioned JSON fixtures; never uses a network."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def _key(request: Any) -> str:
    try:
        return json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("request is not a JSON-serializable object") from None


class RecordedProvider:
    """Answers only requests that exactly match a recorded request."""

    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self._records = records

    @classmethod
    def from_path(cls, path: str | Path | None) -> RecordedProvider:
        if path is None or str(path) == "":
            raise ValueError("recorded provider configuration path is missing")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError("recorded provider configuration file not found") from None
        except (OSError, UnicodeDecodeError):
            raise ValueError("recorded provider configuration file is unreadable") from None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError("recorded provider configuration is not valid JSON") from None
        return cls._from_data(data)

    @classmethod
    def _from_data(cls, data: Any) -> RecordedProvider:
        if not isinstance(data, dict):
            raise ValueError("recorded provider configuration must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version; expected {SCHEMA_VERSION!r}")
        if set(data) != {"schema_version", "records"} or not isinstance(data["records"], list):
            raise ValueError("recorded provider configuration must have 'records' as a list")
        records: dict[str, dict[str, Any]] = {}
        for entry in data["records"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"request", "result"}
                or not isinstance(entry["request"], dict)
                or not isinstance(entry["result"], dict)
            ):
                raise ValueError("each record needs object 'request' and object 'result' only")
            key = _key(entry["request"])
            if key in records:
                raise ValueError("duplicate recorded request")
            records[key] = entry["result"]
        return cls(records)

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        key = _key(request)
        if key not in self._records:
            raise ValueError("no recorded result matches this request")
        try:
            return copy.deepcopy(self._records[key])
        except RecursionError:
            raise ValueError("recorded provider result exceeds supported nesting") from None
