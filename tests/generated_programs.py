"""Scripted generator answers for offline tests: a realistic first draft and two repairs.

``STALE_PLACE`` places with the observation it picked with, which is stale by then. ``NO_RESUME``
observes again before placing but always picks first, so a retry while holding is rejected.
``TRANSFER`` handles both and maps rejections to protocol diagnostics.
"""

from typing import Any

STALE_PLACE = """
def run(tools, payload):
    obs = payload["observation"]
    est = obs["object_pose_estimate"]
    target = obs["target_pose"]
    picked = tools.call("pick", {
        "observation_id": obs["observation_id"], "x_m": est["x_m"], "y_m": est["y_m"],
        "yaw_deg": est["yaw_deg"], "grasp_height_m": target["z_m"],
    })["result"]
    if not picked.get("held"):
        return {"outputs": {"released": False}, "failure": "misgrasp"}
    placed = tools.call("place", {
        "observation_id": obs["observation_id"], "x_m": target["x_m"], "y_m": target["y_m"],
        "z_m": target["z_m"], "yaw_deg": target["yaw_deg"], "approach": "vertical",
    })["result"]
    if placed["status"] != "ok":
        return {"outputs": {"released": False}, "failure": "placement_failure"}
    return {"outputs": {"released": True}, "failure": None}
"""

NO_RESUME = """
def run(tools, payload):
    obs = payload["observation"]
    est = obs["object_pose_estimate"]
    picked = tools.call("pick", {
        "observation_id": obs["observation_id"], "x_m": est["x_m"], "y_m": est["y_m"],
        "yaw_deg": est["yaw_deg"], "grasp_height_m": obs["target_pose"]["z_m"],
    })["result"]
    if picked["status"] == "rejected":
        return {"outputs": {"released": False}, "failure": "tool_rejected"}
    if not picked.get("held"):
        return {"outputs": {"released": False}, "failure": "misgrasp"}
    seen = tools.call("observe", {})["result"]
    if seen["status"] != "ok":
        return {"outputs": {"released": False}, "failure": "missing_observation"}
    fresh = seen["observation"]
    target = fresh["target_pose"]
    placed = tools.call("place", {
        "observation_id": fresh["observation_id"], "x_m": target["x_m"], "y_m": target["y_m"],
        "z_m": target["z_m"], "yaw_deg": target["yaw_deg"], "approach": "vertical",
    })["result"]
    if placed["status"] == "rejected" and placed.get("reason") == "unreachable_target":
        return {"outputs": {"released": False}, "failure": "unreachable_target"}
    if placed["status"] != "ok":
        return {"outputs": {"released": False}, "failure": "placement_failure"}
    return {"outputs": {"released": True}, "failure": None}
"""

TRANSFER = '''
"""Pick the box (unless already held), observe again, place it at the target."""

FAILED = {"released": False}


def diagnose(reply, default):
    if reply.get("reason") == "unreachable_target":
        return "unreachable_target"
    return "tool_rejected" if reply["status"] == "rejected" else default


def run(tools, payload):
    obs = payload["observation"]
    if obs["gripper_state"] != "holding":
        est = obs["object_pose_estimate"]
        picked = tools.call("pick", {
            "observation_id": obs["observation_id"], "x_m": est["x_m"], "y_m": est["y_m"],
            "yaw_deg": est["yaw_deg"], "grasp_height_m": obs["target_pose"]["z_m"],
        })["result"]
        if not picked.get("held"):
            return {"outputs": FAILED, "failure": diagnose(picked, "misgrasp")}
        seen = tools.call("observe", {})["result"]
        if seen["status"] != "ok":
            return {"outputs": FAILED, "failure": "missing_observation"}
        obs = seen["observation"]
    target = obs["target_pose"]
    placed = tools.call("place", {
        "observation_id": obs["observation_id"], "x_m": target["x_m"], "y_m": target["y_m"],
        "z_m": target["z_m"], "yaw_deg": target["yaw_deg"], "approach": "vertical",
    })["result"]
    if placed["status"] != "ok":
        return {"outputs": FAILED, "failure": diagnose(placed, "placement_failure")}
    return {"outputs": {"released": True}, "failure": None}
'''

#: Claims success without acting: the evaluator must not accept it.
CLAIMS_ONLY = """
def run(tools, payload):
    return {"outputs": {"released": True}, "failure": None}
"""

#: Tries to read files and import modules: rejected by static checks, never run.
ESCAPE = """
import os


def run(tools, payload):
    return {"outputs": {"released": open("/etc/passwd").read() != ""}, "failure": None}
"""


def fenced(program: str) -> str:
    return "Here is the program.\n```python\n" + program.strip("\n") + "\n```\n"


def answer(program: str) -> dict[str, Any]:
    return {
        "text": fenced(program),
        "usage": {"input_tokens": 2400, "output_tokens": 600},
        "latency_ms": 9,
    }


class ScriptedGenerator:
    """A provider that answers generator requests from a fixed list of programs."""

    def __init__(self, programs: list[Any]) -> None:
        self.programs = list(programs)
        self.requests: list[dict[str, Any]] = []

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        item = self.programs.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return item
        return answer(item)
