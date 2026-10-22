"""The body model: any robot described as parts, effectors, sensors and capabilities.

The core never classifies limbs. A robot's body is

- **parts**: named groups of joints from the robot's model file (MJCF or URDF), each optionally
  mounted on another part (``mount``), forming a tree;
- **effectors**: named points that touch or hold things, each attached to a part (a wrist gripper,
  a jaw on a neck, a foot);
- **sensors**: cameras and other sensors, each mounted on a part (or on the world);
- **capabilities**: what the body can do (``locomote``, ``reach``, ``grasp``, ``see``, and the
  implicit ``stop``), each provided by a set of parts, for one effector or sensor where it applies.

Skills and task packs ask for capabilities, not parts: "an effector that can reach and grasp" is
satisfied by a single arm's gripper, a humanoid's hand or a quadruped's jaw. Familiar components
and ready-made configurations (``uzumaki.robots``) compile to this model; nothing in the core
depends on which.

``PartClaims`` admits concurrent tool calls only when the parts they move do not overlap; ``stop``
claims every part and preempts whatever holds one.

A ``BodyModel`` is immutable and validated on construction; ``document()`` is its canonical JSON
form and ``sha256`` identifies it in evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
#: Capabilities with a fixed meaning. Others may be declared under the same name pattern.
STANDARD_CAPABILITIES = ("locomote", "reach", "grasp", "see", "stop")
#: Capabilities that act through an effector, and those that read a sensor.
EFFECTOR_CAPABILITIES = frozenset({"reach", "grasp"})
SENSOR_CAPABILITIES = frozenset({"see"})
EFFECTOR_KINDS = frozenset({"gripper", "hand", "jaw", "foot", "suction", "tool"})
SENSOR_KINDS = frozenset({"camera", "depth_camera", "force", "contact"})


class BodyError(ValueError):
    """The body description is invalid; the message names the first problem."""


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise BodyError(f"{what} name {value!r} must match {NAME.pattern}")
    return value


@dataclass(frozen=True)
class Part:
    """A named group of joints. ``mount`` is the part it is attached to (``None``: the root)."""

    name: str
    joints: tuple[str, ...] = ()
    mount: str | None = None
    #: Free-form label for people and presets ("arm", "leg", "neck"); the core never reads it.
    label: str = ""


@dataclass(frozen=True)
class Effector:
    """A point that touches or holds things. ``joints`` actuate it (a gripper's fingers)."""

    name: str
    part: str
    kind: str = "gripper"
    joints: tuple[str, ...] = ()
    site: str | None = None


@dataclass(frozen=True)
class Sensor:
    name: str
    part: str | None
    kind: str = "camera"
    site: str | None = None


@dataclass(frozen=True)
class Capability:
    """``name`` provided by ``parts``, for one ``effector`` or ``sensor`` where that applies."""

    name: str
    parts: tuple[str, ...]
    effector: str | None = None
    sensor: str | None = None

    @property
    def key(self) -> str:
        target = self.effector or self.sensor
        return self.name if target is None else f"{self.name}@{target}"


@dataclass(frozen=True)
class BodyModel:
    """A validated body. Construction raises ``BodyError`` on any inconsistency."""

    name: str
    parts: tuple[Part, ...]
    effectors: tuple[Effector, ...] = ()
    sensors: tuple[Sensor, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    model_file: str | None = None
    configuration: str | None = None
    _index: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _name(self.name.replace("-", "_"), "body")
        parts = {p.name: p for p in self.parts}
        if not parts:
            raise BodyError("a body needs at least one part")
        if len(parts) != len(self.parts):
            raise BodyError("part names must be unique")
        joints: dict[str, str] = {}
        for part in self.parts:
            _name(part.name, "part")
            if part.mount is not None and part.mount not in parts:
                raise BodyError(f"part {part.name!r} is mounted on unknown part {part.mount!r}")
            for joint in part.joints:
                if not isinstance(joint, str) or not joint:
                    raise BodyError(f"part {part.name!r} has an empty joint name")
                if joint in joints:
                    raise BodyError(
                        f"joint {joint!r} is in parts {joints[joint]!r} and {part.name!r}"
                    )
                joints[joint] = part.name
        for part in self.parts:  # the mount relation is a tree
            seen, current = {part.name}, part.mount
            while current is not None:
                if current in seen:
                    raise BodyError(f"part {part.name!r} is in a mount cycle")
                seen.add(current)
                current = parts[current].mount
        names = set(parts)
        effectors = {e.name: e for e in self.effectors}
        sensors = {s.name: s for s in self.sensors}
        for effector in self.effectors:
            _name(effector.name, "effector")
            if effector.part not in parts:
                raise BodyError(f"effector {effector.name!r} is on unknown part {effector.part!r}")
            if effector.kind not in EFFECTOR_KINDS:
                raise BodyError(f"effector {effector.name!r} has unknown kind {effector.kind!r}")
            for joint in effector.joints:
                if joint in joints:
                    raise BodyError(f"effector joint {joint!r} is also in part {joints[joint]!r}")
                joints[joint] = effector.name
        for sensor in self.sensors:
            _name(sensor.name, "sensor")
            if sensor.part is not None and sensor.part not in parts:
                raise BodyError(f"sensor {sensor.name!r} is on unknown part {sensor.part!r}")
            if sensor.kind not in SENSOR_KINDS:
                raise BodyError(f"sensor {sensor.name!r} has unknown kind {sensor.kind!r}")
        if len(effectors) != len(self.effectors) or len(sensors) != len(self.sensors):
            raise BodyError("effector and sensor names must be unique")
        if names & set(effectors) or names & set(sensors) or set(effectors) & set(sensors):
            raise BodyError("parts, effectors and sensors need distinct names")
        keys: set[str] = set()
        for capability in self.capabilities:
            _name(capability.name, "capability")
            if capability.name == "stop":
                raise BodyError("stop is implicit: it claims every part")
            unknown = set(capability.parts) - names
            if unknown:
                raise BodyError(
                    f"capability {capability.key!r} uses unknown parts {sorted(unknown)}"
                )
            if capability.name in EFFECTOR_CAPABILITIES and capability.effector is None:
                raise BodyError(f"capability {capability.name!r} needs an effector")
            if capability.name in SENSOR_CAPABILITIES and capability.sensor is None:
                raise BodyError(f"capability {capability.name!r} needs a sensor")
            if capability.effector is not None and capability.effector not in effectors:
                raise BodyError(f"capability {capability.key!r} names an unknown effector")
            if capability.sensor is not None and capability.sensor not in sensors:
                raise BodyError(f"capability {capability.key!r} names an unknown sensor")
            if capability.key in keys:
                raise BodyError(f"capability {capability.key!r} is declared twice")
            keys.add(capability.key)
        self._index.update(parts=parts, effectors=effectors, sensors=sensors, joints=joints)

    # ------------------------------------------------------------------ queries
    @property
    def joint_names(self) -> frozenset[str]:
        return frozenset(self._index["joints"])

    def capability(self, name: str, target: str | None = None) -> Capability | None:
        if name == "stop":
            return Capability("stop", tuple(p.name for p in self.parts))
        for capability in self.capabilities:
            if capability.name == name and target in (None, capability.effector, capability.sensor):
                return capability
        return None

    def provides(self, name: str, target: str | None = None) -> bool:
        return self.capability(name, target) is not None

    def effectors_with(self, capabilities: Iterable[str]) -> tuple[str, ...]:
        """Effectors with every capability in ``capabilities`` (for example reach and grasp)."""
        wanted = set(capabilities)
        return tuple(
            e.name for e in self.effectors if all(self.provides(c, e.name) for c in wanted)
        )

    def claims(self, name: str, target: str | None = None) -> frozenset[str]:
        """The parts a call using this capability occupies."""
        capability = self.capability(name, target)
        if capability is None:
            raise BodyError(f"body {self.name!r} has no capability {name!r} for {target!r}")
        return frozenset(capability.parts)

    def check_model_names(self, joints: Iterable[str], sites: Iterable[str] = ()) -> list[str]:
        """Names this body uses that the robot's model file does not define (empty when sound)."""
        available, site_names = set(joints), set(sites)
        missing = [f"joint {j}" for j in sorted(self.joint_names - available)]
        for site in [e.site for e in self.effectors] + [s.site for s in self.sensors]:
            if site is not None and site not in site_names:
                missing.append(f"site {site}")
        return missing

    # ------------------------------------------------------------------ identity
    def document(self) -> dict[str, Any]:
        return {
            "schema": "uzumaki.body/1.0",
            "name": self.name,
            "configuration": self.configuration,
            "model_file": self.model_file,
            "parts": [
                {"name": p.name, "joints": list(p.joints), "mount": p.mount, "label": p.label}
                for p in self.parts
            ],
            "effectors": [
                {
                    "name": e.name,
                    "part": e.part,
                    "kind": e.kind,
                    "joints": list(e.joints),
                    "site": e.site,
                }
                for e in self.effectors
            ],
            "sensors": [
                {"name": s.name, "part": s.part, "kind": s.kind, "site": s.site}
                for s in self.sensors
            ],
            "capabilities": [
                {"name": c.name, "parts": list(c.parts), "effector": c.effector, "sensor": c.sensor}
                for c in self.capabilities
            ],
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def summary(self) -> str:
        """One line per capability, for people and conformance output."""
        lines = [f"{self.name} ({self.configuration or 'custom'})"]
        for capability in (*self.capabilities, self.capability("stop")):
            assert capability is not None
            lines.append(f"  {capability.key}: {', '.join(capability.parts) or '-'}")
        return "\n".join(lines)


class ClaimConflict(Exception):
    """A call needs parts another call holds."""

    def __init__(self, call_id: str, holders: Mapping[str, str]) -> None:
        super().__init__(f"call {call_id} needs parts held by {sorted(set(holders.values()))}")
        self.call_id = call_id
        self.holders = dict(holders)


class PartClaims:
    """Which call holds which part. A call is admitted only if none of its parts are held;
    ``stop`` (``preempt=True``) takes every part and returns the calls it preempted."""

    def __init__(self, body: BodyModel) -> None:
        self.body = body
        self._held: dict[str, str] = {}

    @property
    def held(self) -> dict[str, str]:
        return dict(self._held)

    def acquire(self, call_id: str, parts: Iterable[str], *, preempt: bool = False) -> list[str]:
        wanted = frozenset(parts)
        unknown = wanted - {p.name for p in self.body.parts}
        if unknown:
            raise BodyError(f"call {call_id} claims unknown parts {sorted(unknown)}")
        holders = {p: self._held[p] for p in wanted if p in self._held}
        if holders and not preempt:
            raise ClaimConflict(call_id, holders)
        preempted = sorted(set(holders.values()))
        for victim in preempted:
            self.release(victim)
        for part in wanted:
            self._held[part] = call_id
        return preempted

    def release(self, call_id: str) -> None:
        self._held = {p: c for p, c in self._held.items() if c != call_id}
