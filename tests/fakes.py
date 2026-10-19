"""Fake clock, scripted provider and scripted tools for fault-injection tests (no real time)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uzumaki.budgets import Budgets
from uzumaki.evidence import ToolResult

Step = Callable[[], Any]
# Progress the fake environment observes when a tool's effect happens.
ENV_PROGRESS = {"pick": "partial", "place": "goal_reached"}


class FakeClock:
    def __init__(self, start: int = 1000) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


class ScriptedProvider:
    """Plays one scripted step per request; each step returns a result or raises."""

    def __init__(self, steps: list[Step], repeat_last: bool = False) -> None:
        self.steps = list(steps)
        self.repeat_last = repeat_last
        self.requests: list[dict[str, Any]] = []

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if len(self.steps) > 1 or not self.repeat_last:
            step = self.steps.pop(0)
        else:
            step = self.steps[0]
        result: dict[str, Any] = step()
        return result

    @property
    def calls(self) -> int:
        return len(self.requests)


class ScriptedTools:
    """Scripted tool executor that also plays the environment: once a tool returns, it reports
    the progress it observed to ``observer`` with source ``environment`` (not the harness logic).
    """

    def __init__(
        self, steps: list[Step] | None = None, progress: dict[str, str] | None = None
    ) -> None:
        self.steps = list(steps or [])
        self.calls: list[tuple[str, str]] = []
        self.progress = dict(ENV_PROGRESS if progress is None else progress)
        self.observer: Callable[[str, str], None] | None = None

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        self.calls.append((call_id, tool))
        if self.steps:
            result: ToolResult = self.steps.pop(0)()
        else:
            result = ToolResult("ok", {"tool": tool}, {})
        if self.observer is not None and tool in self.progress:
            self.observer(self.progress[tool], "environment")
        return result


def ok(result: dict[str, Any], clock: FakeClock | None = None, took_ms: int = 0) -> Step:
    def step() -> dict[str, Any]:
        if clock is not None:
            clock.advance(took_ms)
        return result

    return step


def fail(exc: BaseException, clock: FakeClock | None = None, took_ms: int = 0) -> Step:
    def step() -> Any:
        if clock is not None:
            clock.advance(took_ms)
        raise exc

    return step


def budgets(**overrides: Any) -> Budgets:
    values: dict[str, Any] = {
        "wall_ms": 120_000,
        "model_calls": 20,
        "steps": 10,
        "retries": 3,
        "max_output_bytes": 4096,
        "max_total_output_bytes": 65536,
    }
    values.update(overrides)
    return Budgets(**values)
