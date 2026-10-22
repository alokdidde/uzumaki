"""Familiar components and ready-made configurations that compile to the body model.

Three layers, and a robot builder works at the highest one that fits:

1. **Configurations**: common robots with named handles: ``SingleArm``, ``TwoArms``,
   ``MobileManipulator``, ``Humanoid``, ``Quadruped``, ``QuadrupedWithArm``,
   ``QuadrupedWithJaw``.
2. **Components**: ``Arm``, ``Gripper`` and ``Hand``, ``Legs``, ``WheeledBase``, ``Torso``,
   ``Neck``, ``Head`` and ``Camera``, assembled freely with ``Robot``. Any component can mount on
   any part-like component (``on=``): a ``Gripper`` on a ``Neck`` is a jaw, an ``Arm`` on a
   quadruped's ``Torso`` gives a Spot-style robot.
3. **The body model** (``uzumaki.body``): parts, effectors, sensors and capabilities.

``Robot.body()`` compiles the components to a validated ``BodyModel``:

- each part-like component is one part (``Legs`` is one part per leg, ``<name>_1`` ...);
- each ``Gripper``/``Hand`` is an effector with ``grasp`` (claiming the part it is on) and
  ``reach`` (claiming every jointed reaching part it is mounted through: arms, necks, heads and
  torsos, up to the first legs, base or root; a rigid torso is not claimed);
- all legs and wheeled bases together provide ``locomote``;
- each ``Camera`` is a sensor with ``see``, which claims no parts.

Joint and site names are the robot's model-file names; ``BodyModel.check_model_names`` checks
them against the file, which the adapter loads.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from uzumaki.body import BodyError, BodyModel, Capability, Effector, Part, Sensor

#: Part labels whose joints move an effector; legs and bases carry it but do not aim it.
REACHING = frozenset({"arm", "neck", "head", "torso"})


# ---------------------------------------------------------------------------- components
@dataclass(frozen=True)
class Arm:
    joints: tuple[str, ...]
    on: str | None = None
    label = "arm"


@dataclass(frozen=True)
class Torso:
    joints: tuple[str, ...] = ()
    on: str | None = None
    label = "torso"


@dataclass(frozen=True)
class Neck:
    joints: tuple[str, ...]
    on: str | None = None
    label = "neck"


@dataclass(frozen=True)
class Head:
    joints: tuple[str, ...] = ()
    on: str | None = None
    label = "head"


@dataclass(frozen=True)
class WheeledBase:
    joints: tuple[str, ...] = ()
    on: str | None = None
    label = "base"


@dataclass(frozen=True)
class Legs:
    """``chains`` holds one joint tuple per leg (2, 4 or 6 legs). The gait runs in the adapter."""

    chains: tuple[tuple[str, ...], ...]
    on: str | None = None
    gait: str = "walk"
    label = "leg"


@dataclass(frozen=True)
class Gripper:
    joints: tuple[str, ...] = ()
    site: str | None = None
    on: str | None = None
    kind: str = "gripper"


@dataclass(frozen=True)
class Hand(Gripper):
    kind: str = "hand"


@dataclass(frozen=True)
class Camera:
    site: str | None = None
    on: str | None = None
    kind: str = "camera"


PartComponent = Arm | Torso | Neck | Head | WheeledBase | Legs
Component = PartComponent | Gripper | Camera
_PART_TYPES = (Arm, Torso, Neck, Head, WheeledBase, Legs)


def _tuple(value: Any) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


# ---------------------------------------------------------------------------- robots
class Robot:
    """A robot assembled from named components. Components are reachable as attributes
    (``robot.arm``, ``robot.left``), and ``body()`` compiles them to a ``BodyModel``."""

    configuration: str | None = None

    def __init__(self, name: str, *, model: str | None = None, **components: Any) -> None:
        self.name = name
        self.model = model
        self.components: dict[str, Component] = {}
        for key, component in components.items():
            if component is None:
                continue
            if not isinstance(component, (*_PART_TYPES, Gripper, Camera)):
                raise BodyError(f"{key!r} is not a component")
            self.components[key] = component

    def __getattr__(self, name: str) -> Any:
        components = self.__dict__.get("components", {})
        if name in components:
            return components[name]
        raise AttributeError(name)

    @property
    def handles(self) -> tuple[str, ...]:
        return tuple(self.components)

    def _part_names(self, key: str) -> tuple[str, ...]:
        component = self.components[key]
        if isinstance(component, Legs):
            return tuple(f"{key}_{i + 1}" for i in range(len(component.chains)))
        return (key,)

    def _mount(self, key: str, on: str | None) -> str | None:
        if on is None:
            return None
        target = self.components.get(on)
        if target is None or not isinstance(target, _PART_TYPES):
            raise BodyError(f"{key!r} is mounted on {on!r}, which is not a part of this robot")
        if isinstance(target, Legs):
            raise BodyError(f"{key!r} cannot mount on legs {on!r}; mount it on a torso")
        return on

    def _effector_part(self, key: str, effector: Gripper) -> str:
        if effector.on is not None:
            mount = self._mount(key, effector.on)
            assert mount is not None
            return mount
        arms = [k for k, c in self.components.items() if isinstance(c, Arm | Neck)]
        if len(arms) != 1:
            raise BodyError(f"say which part {key!r} is on (on=...)")
        return arms[0]

    def body(self) -> BodyModel:
        parts: list[Part] = []
        labels: dict[str, str] = {}
        for key, component in self.components.items():
            if not isinstance(component, _PART_TYPES):
                continue
            mount = self._mount(key, component.on)
            if isinstance(component, Legs):
                if len(component.chains) not in (2, 4, 6):
                    raise BodyError(f"legs {key!r} need 2, 4 or 6 chains")
                for name, chain in zip(self._part_names(key), component.chains, strict=True):
                    parts.append(Part(name, _tuple(chain), mount, "leg"))
                    labels[name] = "leg"
            else:
                parts.append(Part(key, _tuple(component.joints), mount, component.label))
                labels[key] = component.label
        mounts = {p.name: p.mount for p in parts}
        effectors: list[Effector] = []
        sensors: list[Sensor] = []
        capabilities: list[Capability] = []
        for key, component in self.components.items():
            if isinstance(component, Gripper):
                part = self._effector_part(key, component)
                effectors.append(
                    Effector(key, part, component.kind, _tuple(component.joints), component.site)
                )
                jointed = {p.name for p in parts if p.joints}
                reaching: list[str] = []
                current: str | None = part
                while current is not None and labels.get(current) in REACHING:
                    if current in jointed:  # a rigid torso or head carries but does not move
                        reaching.append(current)
                    current = mounts[current]
                if reaching:
                    capabilities.append(Capability("reach", tuple(reaching), effector=key))
                capabilities.append(Capability("grasp", (part,), effector=key))
            elif isinstance(component, Camera):
                mount = self._mount(key, component.on)
                sensors.append(Sensor(key, mount, component.kind, component.site))
                capabilities.append(Capability("see", (), sensor=key))
        moving = tuple(p.name for p in parts if labels[p.name] in ("leg", "base"))
        if moving:
            capabilities.insert(0, Capability("locomote", moving))
        return BodyModel(
            name=self.name,
            parts=tuple(parts),
            effectors=tuple(effectors),
            sensors=tuple(sensors),
            capabilities=tuple(capabilities),
            model_file=self.model,
            configuration=self.configuration,
        )


def _on(component: Any, mount: str | None) -> Any:
    """``component`` mounted on ``mount`` unless it already names a mount."""
    if component is None or mount is None or component.on is not None:
        return component
    return dataclasses.replace(component, on=mount)


# ---------------------------------------------------------------------------- configurations
class SingleArm(Robot):
    """One arm with a gripper, an optional camera. Handles: ``arm``, ``gripper``, ``camera``."""

    configuration = "single_arm"

    def __init__(
        self,
        name: str = "single_arm",
        *,
        model: str | None = None,
        arm: Arm,
        gripper: Gripper,
        camera: Camera | None = None,
    ) -> None:
        super().__init__(name, model=model, arm=arm, gripper=_on(gripper, "arm"), camera=camera)


class TwoArms(Robot):
    """Two arms with grippers, optionally on a torso. Handles: ``left``, ``right``,
    ``left_gripper``, ``right_gripper``, ``torso``, ``camera``."""

    configuration = "two_arms"

    def __init__(
        self,
        name: str = "two_arms",
        *,
        model: str | None = None,
        left: Arm,
        right: Arm,
        left_gripper: Gripper,
        right_gripper: Gripper,
        torso: Torso | None = None,
        camera: Camera | None = None,
    ) -> None:
        mount = "torso" if torso is not None else None
        super().__init__(
            name,
            model=model,
            torso=torso,
            left=_on(left, mount),
            right=_on(right, mount),
            left_gripper=_on(left_gripper, "left"),
            right_gripper=_on(right_gripper, "right"),
            camera=camera,
        )


class MobileManipulator(Robot):
    """A wheeled base carrying one arm. Handles: ``base``, ``arm``, ``gripper``, ``camera``."""

    configuration = "mobile_manipulator"

    def __init__(
        self,
        name: str = "mobile_manipulator",
        *,
        model: str | None = None,
        base: WheeledBase,
        arm: Arm,
        gripper: Gripper,
        camera: Camera | None = None,
    ) -> None:
        super().__init__(
            name,
            model=model,
            base=base,
            arm=_on(arm, "base"),
            gripper=_on(gripper, "arm"),
            camera=camera,
        )


class Humanoid(Robot):
    """Two arms with hands and two legs on a torso, a head with a camera. Handles: ``torso``,
    ``left``, ``right``, ``left_hand``, ``right_hand``, ``legs``, ``head``, ``camera``."""

    configuration = "humanoid"

    def __init__(
        self,
        name: str = "humanoid",
        *,
        model: str | None = None,
        torso: Torso,
        left: Arm,
        right: Arm,
        left_hand: Gripper,
        right_hand: Gripper,
        legs: Legs,
        head: Head | None = None,
        camera: Camera | None = None,
    ) -> None:
        if len(legs.chains) != 2:
            raise BodyError("a humanoid has two legs")
        super().__init__(
            name,
            model=model,
            torso=torso,
            left=_on(left, "torso"),
            right=_on(right, "torso"),
            left_hand=_on(left_hand, "left"),
            right_hand=_on(right_hand, "right"),
            legs=_on(legs, "torso"),
            head=_on(head, "torso"),
            camera=_on(camera, "head" if head is not None else None),
        )


class Quadruped(Robot):
    """Four legs on a torso, an optional head and camera. Handles: ``torso``, ``legs``,
    ``head``, ``camera``."""

    configuration = "quadruped"

    def __init__(
        self,
        name: str = "quadruped",
        *,
        model: str | None = None,
        legs: Legs,
        torso: Torso | None = None,
        head: Head | None = None,
        camera: Camera | None = None,
        **extra: Any,
    ) -> None:
        if len(legs.chains) != 4:
            raise BodyError("a quadruped has four legs")
        torso = torso or Torso()
        super().__init__(
            name,
            model=model,
            torso=torso,
            legs=_on(legs, "torso"),
            head=_on(head, "torso"),
            camera=_on(camera, "head" if head is not None else "torso"),
            **extra,
        )


class QuadrupedWithArm(Quadruped):
    """A quadruped with an arm on its back. Adds handles ``arm`` and ``gripper``."""

    configuration = "quadruped_with_arm"

    def __init__(
        self,
        name: str = "quadruped_with_arm",
        *,
        model: str | None = None,
        legs: Legs,
        arm: Arm,
        gripper: Gripper,
        torso: Torso | None = None,
        head: Head | None = None,
        camera: Camera | None = None,
    ) -> None:
        super().__init__(
            name,
            model=model,
            legs=legs,
            torso=torso,
            head=head,
            camera=camera,
            arm=_on(arm, "torso"),
            gripper=_on(gripper, "arm"),
        )


class QuadrupedWithJaw(Quadruped):
    """A quadruped that grips with a jaw on its neck. Adds handles ``neck`` and ``jaw``."""

    configuration = "quadruped_with_jaw"

    def __init__(
        self,
        name: str = "quadruped_with_jaw",
        *,
        model: str | None = None,
        legs: Legs,
        neck: Neck,
        jaw: Gripper,
        torso: Torso | None = None,
        camera: Camera | None = None,
    ) -> None:
        if jaw.kind == "gripper":
            jaw = dataclasses.replace(jaw, kind="jaw")
        super().__init__(
            name,
            model=model,
            legs=legs,
            torso=torso,
            camera=_on(camera, "neck"),
            neck=_on(neck, "torso"),
            jaw=_on(jaw, "neck"),
        )


CONFIGURATIONS: dict[str, type[Robot]] = {
    str(cls.configuration): cls
    for cls in (
        SingleArm,
        TwoArms,
        MobileManipulator,
        Humanoid,
        Quadruped,
        QuadrupedWithArm,
        QuadrupedWithJaw,
    )
}
