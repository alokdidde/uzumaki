"""Task packs: what a task asks of a body and how success is judged.

A ``TaskPack`` bundles everything task-specific: the capabilities it needs on one effector, the
tools it uses and the capability behind each, the planner's ``TaskCard``, the capability specs the
generator may be asked for, and the episode runner with its independent evaluator. It never names
a robot; ``bind`` matches it to a ``BodyModel`` and fixes which effector it uses and which parts
each tool call claims. A pack runs on every body that has an effector with the capabilities it
requires, so ``pick-place`` runs on a single arm, either hand of a humanoid or a quadruped's jaw.

Case sets belong to the embodiment, because what a robot can reach depends on its body
(``uzumaki.embodiments``); the pack's evaluator and outcome rules stay the same for all of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from uzumaki.body import BodyError, BodyModel
from uzumaki.broker import PICK_PLACE_V1_TOOL_VERSION
from uzumaki.generator import TRANSFER_BOX, CapabilitySpec
from uzumaki.planner import PICK_PLACE_CARD, TaskCard


class TaskMismatch(BodyError):
    """The body cannot do what the task needs; the message says what is missing."""


@dataclass(frozen=True)
class TaskPack:
    name: str
    protocol: str
    about: str
    #: Capabilities one effector must have (an empty set: the task needs no effector).
    requires: frozenset[str]
    #: Tool name -> the capability it uses (``None``: it reads state and claims no part).
    tools: Mapping[str, str | None]
    tool_version: str
    card: TaskCard
    specs: tuple[CapabilitySpec, ...] = ()
    #: Further capabilities the body must have anywhere (for example locomote).
    also_requires: frozenset[str] = field(default_factory=frozenset)
    #: The pack's hand-authored skill set (``uzumaki.skillset/1.0``), relative to the repository.
    skill_set: str | None = None

    @property
    def tool_catalog(self) -> dict[str, str]:
        """The tool catalog the skill registry checks skill permissions against."""
        return {name: self.tool_version for name in self.tools}

    def bind(self, body: BodyModel, effector: str | None = None) -> TaskBinding:
        """Match this task to ``body``: pick the effector and fix each tool's part claims."""
        candidates = body.effectors_with(self.requires) if self.requires else ()
        if self.requires:
            if effector is not None and effector not in candidates:
                raise TaskMismatch(
                    f"effector {effector!r} of {body.name!r} lacks {sorted(self.requires)}"
                )
            if not candidates:
                raise TaskMismatch(
                    f"{body.name!r} has no effector with {sorted(self.requires)} for {self.name!r}"
                )
            effector = effector or candidates[0]
        missing = sorted(c for c in self.also_requires if not body.provides(c))
        if missing:
            raise TaskMismatch(f"{body.name!r} lacks {missing} for {self.name!r}")
        claims: dict[str, frozenset[str]] = {}
        for tool, capability in self.tools.items():
            if capability is None:
                claims[tool] = frozenset()
            elif capability in ("reach", "grasp"):
                claims[tool] = body.claims("reach", effector) | body.claims("grasp", effector)
            else:
                claims[tool] = body.claims(capability)
        return TaskBinding(self, body, effector, MappingProxyType(claims))


@dataclass(frozen=True)
class TaskBinding:
    """A task pack matched to one body: the effector it uses and the parts each tool claims."""

    task: TaskPack
    body: BodyModel
    effector: str | None
    claims: Mapping[str, frozenset[str]]


#: Rigid-object pick-and-place on a tabletop (protocol ``pick-place-v1``): ``pick`` and
#: ``place`` reach and grasp with one effector, ``observe`` reads state, ``abort`` stops.
PICK_PLACE = TaskPack(
    name="pick-place",
    protocol="pick-place-v1",
    about="move a box to a target pose on a tabletop and release it",
    requires=frozenset({"reach", "grasp"}),
    tools=MappingProxyType({"observe": None, "pick": "grasp", "place": "grasp", "abort": "stop"}),
    tool_version=PICK_PLACE_V1_TOOL_VERSION,
    card=PICK_PLACE_CARD,
    specs=(TRANSFER_BOX,),
    skill_set="tests/fixtures/skills_pick_place_v1.json",
)

TASKS: Mapping[str, TaskPack] = MappingProxyType({PICK_PLACE.name: PICK_PLACE})


def task_pack(name: str) -> TaskPack:
    pack = TASKS.get(name)
    if pack is None:
        raise KeyError(f"unknown task pack {name!r}; known: {', '.join(sorted(TASKS))}")
    return pack
