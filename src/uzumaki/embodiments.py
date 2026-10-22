"""Embodiments: a robot description plus the environment adapter that makes it act.

An ``Embodiment`` names a ``Robot`` (``uzumaki.robots``; it compiles to the body model), the task
packs it supports, and the module that provides its ``EnvironmentAdapter``. Adapters are loaded
only when asked for, so a simulator or hardware bridge never becomes a dependency of the core.

An ``EnvironmentAdapter`` gives, for one task pack:

- ``cases(task)``: the case set this body can reach (the evaluator rules come from the pack);
- ``environment(task)``: a factory that builds a fresh ``CaseEnvironment`` (clock, world with the
  task's tool handlers, independent evaluator reading only privileged state) for each case;
- ``model_names()``: the joint and site names in the robot's model file, or ``None`` when there
  is no model file (the scripted fixture), so conformance can check the robot description;
- ``identity()``: versions and hashes that pin a run.

Physical limits and the safety envelope are the adapter's to enforce, on the host side; a skill is
never trusted to respect them. Adding a robot is one ``EMBODIMENTS`` entry and its adapter module.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from uzumaki.fixture_env import FixtureCase, load_cases
from uzumaki.robots import Arm, Gripper, Robot, SingleArm

REPO = Path(__file__).resolve().parents[2]
FIXTURE_CASES = REPO / "tests" / "fixtures" / "baseline_cases_v1.json"


class EnvironmentAdapter(Protocol):
    def cases(self, task: str) -> tuple[FixtureCase, ...]: ...

    def environment(self, task: str) -> Callable[[FixtureCase], Any]: ...

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None: ...

    def identity(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class Embodiment:
    name: str
    robot: Robot
    backend: str
    about: str
    tasks: tuple[str, ...]
    #: ``module:attribute`` of a zero-argument callable returning the ``EnvironmentAdapter``.
    adapter_ref: str

    def adapter(self) -> EnvironmentAdapter:
        module, _, attribute = self.adapter_ref.partition(":")
        factory = getattr(importlib.import_module(module), attribute)
        adapter: EnvironmentAdapter = factory()
        return adapter


#: The SO-101 as a ``SingleArm``, with the joint and site names of the Menagerie model file.
SO101 = SingleArm(
    "so101",
    model="robotstudio_so101/so101.xml",
    arm=Arm(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")),
    gripper=Gripper(("gripper",), site="gripperframe"),
)
#: The Franka Emika Panda as a ``SingleArm``; its ``tcp`` site is added by the MuJoCo scene.
PANDA = SingleArm(
    "panda",
    model="franka_emika_panda/panda.xml",
    arm=Arm(tuple(f"joint{i}" for i in range(1, 8))),
    gripper=Gripper(("finger_joint1", "finger_joint2"), site="tcp"),
)
#: The scripted fixture arm of the offline baseline: exact kinematics, no model file.
FIXTURE_ARM = SingleArm("fixture_arm", arm=Arm(("arm",)), gripper=Gripper(("gripper",)))


class FixtureAdapter:
    """The scripted fixture world and evaluator on the frozen ``pick-place-v1`` cases."""

    def cases(self, task: str) -> tuple[FixtureCase, ...]:
        _only_pick_place(task)
        return load_cases(FIXTURE_CASES)

    def environment(self, task: str) -> Callable[[FixtureCase], Any]:
        from uzumaki.baseline import fixture_environment

        _only_pick_place(task)
        return fixture_environment

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None:
        return None

    def identity(self) -> dict[str, str]:
        return {
            "backend": "fixture",
            "cases_sha256": hashlib.sha256(FIXTURE_CASES.read_bytes()).hexdigest(),
        }


def _only_pick_place(task: str) -> None:
    if task != "pick-place":
        raise KeyError(f"this adapter supports only the pick-place task, not {task!r}")


EMBODIMENTS: Mapping[str, Embodiment] = MappingProxyType(
    {
        "fixture-arm": Embodiment(
            "fixture-arm",
            FIXTURE_ARM,
            "fixture",
            "scripted single arm of the offline baseline (no physics)",
            ("pick-place",),
            "uzumaki.embodiments:FixtureAdapter",
        ),
        "so101": Embodiment(
            "so101",
            SO101,
            "mujoco",
            "Robot Studio SO-101 arm in MuJoCo (needs requirements-sim.txt and assets)",
            ("pick-place",),
            "uzumaki.sim.adapter:SO101Adapter",
        ),
        "panda": Embodiment(
            "panda",
            PANDA,
            "mujoco",
            "Franka Emika Panda arm in MuJoCo (needs requirements-sim.txt and assets)",
            ("pick-place",),
            "uzumaki.sim.adapter:PandaAdapter",
        ),
    }
)


def embodiment(name: str) -> Embodiment:
    found = EMBODIMENTS.get(name)
    if found is None:
        raise KeyError(f"unknown embodiment {name!r}; known: {', '.join(sorted(EMBODIMENTS))}")
    return found
