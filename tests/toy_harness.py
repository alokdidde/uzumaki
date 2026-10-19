"""Deterministic toy harness, fake environment and provider adapter used by evidence tests."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from uzumaki.evidence import (
    DecisionSource,
    EventRecorder,
    Limits,
    ToolExecutor,
    ToolResult,
)
from uzumaki.providers import RecordedProvider

PROVIDER_FIXTURE = Path(__file__).parent / "fixtures" / "events_provider.json"
SKILLS = {"pick-cube": "1.0.0"}
MODELS = {"planner": "fixture-planner-1"}


class ProviderDecisions:
    """Live-path decision source backed by the issue-03 recorded provider."""

    def __init__(self, provider: RecordedProvider) -> None:
        self.provider = provider
        self.calls = 0

    def decide(self, decision_id: str, role: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return self.provider.request(request)


class FakeEnv:
    def __init__(self, observation_id: str = "obs-1", diagnostics: dict[str, Any] | None = None):
        self.observation_id = observation_id
        self.diagnostics = diagnostics or {}
        self.calls: list[str] = []

    def execute(
        self, call_id: str, tool: str, arguments: dict[str, Any], skill_id: str | None
    ) -> ToolResult:
        self.calls.append(tool)
        if tool == "observe":
            result = {"observation_id": self.observation_id, "captured_at_ms": 5, "object": "cube"}
        else:
            result = {"tool": tool, "done": True}
        return ToolResult("ok", result, copy.deepcopy(self.diagnostics))


def run(rec: EventRecorder, decisions: DecisionSource, tools: ToolExecutor) -> dict[str, Any]:
    obs = rec.call_tool(tools, "call-1", "observe", {})
    rec.observation(
        obs.result["observation_id"], "fake_env", obs.result["captured_at_ms"], obs.result
    )
    decision = rec.decide(
        decisions,
        "dec-1",
        "planner",
        {"role": "planner", "observation_id": obs.result["observation_id"]},
    )
    if decision["decision"] == "select":
        rec.call_tool(tools, "call-2", "pick", {"target": "cube"}, skill_id=decision["skill_id"])
        rec.call_tool(tools, "call-3", "place", {"x_m": 0.1, "y_m": 0.0})
        rec.outcome("completed", "goal_reached")
    else:
        rec.outcome("abstained", "none")
    return rec.finish("completed")


class Clock:
    def __init__(self, step: int = 3) -> None:
        self.t = 100
        self.step = step

    def __call__(self) -> int:
        self.t += self.step
        return self.t


def new_recorder(
    limits: Limits | None = None, clock: Callable[[], int] | None = None
) -> EventRecorder:
    return EventRecorder(
        run_id="run-1",
        task_id="pick-place-v1",
        candidate_id="cand-1",
        skill_versions=SKILLS,
        model_versions=MODELS,
        limits=limits,
        clock=clock or Clock(),
    )


def record(observation_id: str = "obs-1", diagnostics: dict[str, Any] | None = None) -> Any:
    provider = RecordedProvider.from_path(PROVIDER_FIXTURE)
    return run(new_recorder(), ProviderDecisions(provider), FakeEnv(observation_id, diagnostics))


def rehash(bundle: dict[str, Any]) -> dict[str, Any]:
    """Recompute a consistent checksum chain, as a forger or buggy writer could."""
    from uzumaki import evidence

    manifest = bundle["manifest"]
    prev = evidence._genesis(manifest)
    for seq, event in enumerate(bundle["events"]):
        event["seq"] = seq
        event["prev_sha256"] = prev
        body = {k: v for k, v in event.items() if k != "sha256"}
        event["sha256"] = evidence._chain(prev, body)
        prev = event["sha256"]
    manifest["event_count"] = len(bundle["events"])
    manifest["head_sha256"] = prev
    return bundle
