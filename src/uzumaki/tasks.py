"""Task packs: what a task asks of a body and how success is judged.

A ``TaskPack`` bundles everything task-specific: the capabilities it needs on one effector, the
tools it uses and the capability behind each, the planner's ``TaskCard``, the capability specs the
generator may be asked for, and the episode runner with its independent evaluator. It never names
a robot; ``bind`` matches it to a ``BodyModel`` and fixes which effector it uses and which parts
each tool call claims. A pack runs on every body that has an effector with the capabilities it
requires, so ``pick-place`` runs on a single arm, either hand of a humanoid or a quadruped's jaw.

A pack that needs several effectors names roles instead: ``handover`` needs a ``giver`` and a
``taker``, two different effectors that can each reach and grasp. ``bind`` assigns one effector
per role (in the body's order unless given), and each tool claims the parts of the roles it uses,
so ``handover`` occupies both arms while ``pick`` occupies only the giver's.

Case sets belong to the embodiment, because what a robot can reach depends on its body
(``uzumaki.embodiments``); the pack's evaluator and outcome rules stay the same for all of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from uzumaki.body import BodyError, BodyModel
from uzumaki.broker import PICK_PLACE_V1_TOOL_VERSION
from uzumaki.fixture_env import FETCH_PROTOCOL, HANDOVER_PROTOCOL, TASK_PROTOCOL
from uzumaki.generator import TRANSFER_BOX, CapabilitySpec
from uzumaki.planner import FETCH_CARD, HANDOVER_CARD, PICK_PLACE_CARD, TaskCard


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
    #: Role -> capabilities its effector must have, for packs that use several effectors (each
    #: role gets a different one); tool -> the roles it uses (default: every role).
    roles: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    tool_roles: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def tool_catalog(self) -> dict[str, str]:
        """The tool catalog the skill registry checks skill permissions against."""
        return {name: self.tool_version for name in self.tools}

    def bind(
        self,
        body: BodyModel,
        effector: str | None = None,
        effectors: Mapping[str, str] | None = None,
    ) -> TaskBinding:
        """Match this task to ``body``: pick the effector (or one per role, ``effectors``
        naming any of them) and fix each tool's part claims."""
        if self.roles:
            return self._bind_roles(body, dict(effectors or {}))
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

    def _bind_roles(self, body: BodyModel, chosen: dict[str, str]) -> TaskBinding:
        assigned: dict[str, str] = {}
        for role, needs in self.roles.items():
            candidates = body.effectors_with(needs)
            effector = chosen.get(role)
            if effector is None:
                taken = set(assigned.values()) | set(chosen.values())
                effector = next((e for e in candidates if e not in taken), None)
            if effector is None or effector not in candidates:
                raise TaskMismatch(
                    f"{body.name!r} has no free effector with {sorted(needs)} for role {role!r}"
                    f" of {self.name!r}"
                )
            if effector in assigned.values():
                raise TaskMismatch(f"roles of {self.name!r} need different effectors")
            assigned[role] = effector
        claims: dict[str, frozenset[str]] = {}
        for tool, capability in self.tools.items():
            if capability is None:
                claims[tool] = frozenset()
            elif capability in ("reach", "grasp"):
                parts: frozenset[str] = frozenset()
                for role in self.tool_roles.get(tool, tuple(self.roles)):
                    parts |= body.claims("reach", assigned[role])
                    parts |= body.claims("grasp", assigned[role])
                claims[tool] = parts
            else:
                claims[tool] = body.claims(capability)
        first = next(iter(assigned.values()))
        return TaskBinding(self, body, first, MappingProxyType(claims), MappingProxyType(assigned))


@dataclass(frozen=True)
class TaskBinding:
    """A task pack matched to one body: the effector it uses (the first role's, for a pack with
    roles), the effector of each role, and the parts each tool claims."""

    task: TaskPack
    body: BodyModel
    effector: str | None
    claims: Mapping[str, frozenset[str]]
    roles: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


#: Rigid-object pick-and-place on a tabletop (protocol ``pick-place-v1``): ``pick`` and
#: ``place`` reach and grasp with one effector, ``observe`` reads state, ``abort`` stops.
PICK_PLACE = TaskPack(
    name="pick-place",
    protocol=TASK_PROTOCOL,
    about="move a box to a target pose on a tabletop and release it",
    requires=frozenset({"reach", "grasp"}),
    tools=MappingProxyType({"observe": None, "pick": "grasp", "place": "grasp", "abort": "stop"}),
    tool_version=PICK_PLACE_V1_TOOL_VERSION,
    card=PICK_PLACE_CARD,
    specs=(TRANSFER_BOX,),
    skill_set="tests/fixtures/skills_pick_place_v1.json",
)

_REACH_GRASP = frozenset({"reach", "grasp"})
#: Two-effector handover (protocol ``handover-v1``): the giver picks the object, ``handover``
#: passes it to the taker (both arms), and the taker places it.
HANDOVER = TaskPack(
    name="handover",
    protocol=HANDOVER_PROTOCOL,
    about="pick an object with one effector, hand it to another, place it at a target pose",
    requires=frozenset(),
    tools=MappingProxyType(
        {"observe": None, "pick": "grasp", "handover": "grasp", "place": "grasp", "abort": "stop"}
    ),
    tool_version=PICK_PLACE_V1_TOOL_VERSION,
    card=HANDOVER_CARD,
    skill_set="tests/fixtures/skills_handover_v1.json",
    roles=MappingProxyType({"giver": _REACH_GRASP, "taker": _REACH_GRASP}),
    tool_roles=MappingProxyType(
        {"pick": ("giver",), "handover": ("giver", "taker"), "place": ("taker",)}
    ),
)

#: Mobile pick-and-place across two tables (protocol ``fetch-v1``): ``go_to`` moves the body
#: (any part that locomotes) until a point is within reach, then one effector picks and places.
FETCH = TaskPack(
    name="fetch",
    protocol=FETCH_PROTOCOL,
    about="fetch a box from one table and place it at a target pose on another",
    requires=frozenset({"reach", "grasp"}),
    tools=MappingProxyType(
        {
            "observe": None,
            "go_to": "locomote",
            "pick": "grasp",
            "place": "grasp",
            "abort": "stop",
        }
    ),
    tool_version=PICK_PLACE_V1_TOOL_VERSION,
    card=FETCH_CARD,
    also_requires=frozenset({"locomote"}),
    skill_set="tests/fixtures/skills_fetch_v1.json",
)

TASKS: Mapping[str, TaskPack] = MappingProxyType(
    {PICK_PLACE.name: PICK_PLACE, HANDOVER.name: HANDOVER, FETCH.name: FETCH}
)


def task_pack(name: str) -> TaskPack:
    pack = TASKS.get(name)
    if pack is None:
        raise KeyError(f"unknown task pack {name!r}; known: {', '.join(sorted(TASKS))}")
    return pack
