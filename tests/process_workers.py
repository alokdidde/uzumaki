"""Module-level targets for real child-process deadline tests (imported by spawned children)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def answer(request: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "abstain", "echo": request.get("n")}


def hang(request: dict[str, Any]) -> dict[str, Any]:
    while True:
        time.sleep(1)


def hang_with_grandchild(request: dict[str, Any]) -> dict[str, Any]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(request["pid_file"]).write_text(str(child.pid), encoding="ascii")
    while True:
        time.sleep(1)


def leave_grandchild(request: dict[str, Any]) -> dict[str, Any]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(request["pid_file"]).write_text(str(child.pid), encoding="ascii")
    return {"decision": "abstain"}


def crash(request: dict[str, Any]) -> dict[str, Any]:
    os._exit(3)


def raise_error(request: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("secret-token-should-not-leak")


def flood(request: dict[str, Any]) -> dict[str, Any]:
    return {"blob": "z" * 1_000_000}


def not_json(request: dict[str, Any]) -> Any:
    return {"value": float("nan")}


def not_object(request: dict[str, Any]) -> Any:
    return [1, 2, 3]
