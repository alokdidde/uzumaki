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
from uzumaki.robots import (
    Arm,
    Gripper,
    Hand,
    Humanoid,
    Legs,
    MobileManipulator,
    QuadrupedWithArm,
    Robot,
    SingleArm,
    Torso,
    TwoArms,
    WheeledBase,
)

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
_VX300S = ("waist", "shoulder", "elbow", "forearm_roll", "wrist_angle", "wrist_rotate")
#: ALOHA (two ViperX 300 S arms facing each other) as ``TwoArms``; the ``tcp`` sites are added by
#: the MuJoCo scene between each gripper's fingertips.
ALOHA = TwoArms(
    "aloha",
    model="aloha/aloha.xml",
    left=Arm(tuple(f"left/{j}" for j in _VX300S)),
    right=Arm(tuple(f"right/{j}" for j in _VX300S)),
    left_gripper=Gripper(("left/left_finger", "left/right_finger"), site="left/tcp"),
    right_gripper=Gripper(("right/left_finger", "right/right_finger"), site="right/tcp"),
)
#: Stanford TidyBot as a ``MobileManipulator``: a holonomic base (two slides and a turn), a Kinova
#: Gen3 arm and a Robotiq 2F-85 gripper; the ``tcp`` site is added by the MuJoCo scene.
TIDYBOT = MobileManipulator(
    "tidybot",
    model="stanford_tidybot/tidybot.xml",
    base=WheeledBase(("joint_x", "joint_y", "joint_th")),
    arm=Arm(tuple(f"joint_{i}" for i in range(1, 8))),
    gripper=Gripper(("right_driver_joint", "left_driver_joint"), site="tcp"),
)
#: Boston Dynamics Spot with its arm as a ``QuadrupedWithArm``; the ``tcp`` site between the jaws
#: is added by the MuJoCo scene.
SPOT = QuadrupedWithArm(
    "spot",
    model="boston_dynamics_spot/spot_arm.xml",
    legs=Legs(tuple((f"{leg}_hx", f"{leg}_hy", f"{leg}_kn") for leg in ("fl", "fr", "hl", "hr"))),
    arm=Arm(("arm_sh0", "arm_sh1", "arm_el0", "arm_el1", "arm_wr0", "arm_wr1")),
    gripper=Gripper(("arm_f1x",), site="tcp"),
)
_G1_ARM = ("shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow", "wrist_roll", "wrist_pitch",
           "wrist_yaw")  # fmt: skip
_G1_HAND = ("thumb_0", "thumb_1", "thumb_2", "index_0", "index_1", "middle_0", "middle_1")
_G1_LEG = ("hip_pitch", "hip_roll", "hip_yaw", "knee", "ankle_pitch", "ankle_roll")
#: Unitree G1 with Dex3 hands as a ``Humanoid``; the ``right_tcp`` and ``left_tcp`` sites in
#: front of each palm are added by the MuJoCo scene.
G1 = Humanoid(
    "g1",
    model="unitree_g1/g1_with_hands.xml",
    torso=Torso(("waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint")),
    left=Arm(tuple(f"left_{j}_joint" for j in _G1_ARM)),
    right=Arm(tuple(f"right_{j}_joint" for j in _G1_ARM)),
    left_hand=Hand(tuple(f"left_hand_{j}_joint" for j in _G1_HAND), site="left_tcp"),
    right_hand=Hand(tuple(f"right_hand_{j}_joint" for j in _G1_HAND), site="right_tcp"),
    legs=Legs(tuple(tuple(f"{side}_{j}_joint" for j in _G1_LEG) for side in ("left", "right"))),
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
        "aloha": Embodiment(
            "aloha",
            ALOHA,
            "mujoco",
            "ALOHA, two ViperX 300 S arms, in MuJoCo (needs requirements-sim.txt and assets)",
            ("handover",),
            "uzumaki.sim.adapter:AlohaAdapter",
        ),
        "tidybot": Embodiment(
            "tidybot",
            TIDYBOT,
            "mujoco",
            "Stanford TidyBot, a mobile base with a Kinova Gen3 arm, in MuJoCo (needs"
            " requirements-sim.txt and assets)",
            ("fetch",),
            "uzumaki.sim.adapter:TidybotAdapter",
        ),
        "spot": Embodiment(
            "spot",
            SPOT,
            "mujoco",
            "Boston Dynamics Spot with its arm, standing on its legs, in MuJoCo (needs"
            " requirements-sim.txt and assets)",
            ("pick-place",),
            "uzumaki.sim.adapter:SpotAdapter",
        ),
        "g1": Embodiment(
            "g1",
            G1,
            "mujoco",
            "Unitree G1 humanoid with Dex3 hands, its pelvis on a stand, in MuJoCo (needs"
            " requirements-sim.txt and assets)",
            ("pick-place",),
            "uzumaki.sim.adapter:G1Adapter",
        ),
    }
)


def embodiment(name: str) -> Embodiment:
    found = EMBODIMENTS.get(name)
    if found is None:
        raise KeyError(f"unknown embodiment {name!r}; known: {', '.join(sorted(EMBODIMENTS))}")
    return found
