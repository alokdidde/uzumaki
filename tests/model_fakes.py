"""Offline stand-ins for a live model behind ``uzumaki.gateway`` (no network)."""

import json
from typing import Any

from uzumaki.faults import TransientProviderError


def context_of(request: dict[str, Any]) -> dict[str, Any]:
    """The JSON data block a ``ModelPlanner`` request carries."""
    content = request["messages"][0]["content"]
    block = content.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    data: dict[str, Any] = json.loads(block)
    return data


def answer(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"text": text, "usage": {"input_tokens": 900, "output_tokens": 40}, "latency_ms": 5}


class CompetentModel:
    """Answers like a model that follows the rules: place when holding, otherwise pick at the
    target's half height. ``script`` overrides the first answers (text, objects or exceptions)."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return answer(item)
        context = context_of(request)
        skills = {card["skill_id"]: card for card in context["eligible_skills"]}
        observation = context["observation"]
        if observation["gripper_state"] == "holding" and "place-box" in skills:
            return answer(
                {"action": "select", "skill_id": "place-box", "skill_version": 1,
                 "parameters": {"approach": "vertical"}, "reason": "holding the box"}
            )  # fmt: skip
        if "pick-box" in skills and observation["target_pose"] is not None:
            height = observation["target_pose"]["z_m"]
            return answer(
                {"action": "select", "skill_id": "pick-box", "skill_version": 1,
                 "parameters": {"grasp_height_m": height}, "reason": "box not held"}
            )  # fmt: skip
        return answer({"action": "abstain", "reason": "no fitting skill"})


RATE_LIMITED = TransientProviderError("provider_unavailable")
