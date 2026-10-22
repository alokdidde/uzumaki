"""Environment adapters for the simulated robots in MuJoCo (``uzumaki.embodiments``): ``so101``
and ``panda`` are one ``ArmAdapter`` over the arm's ``ArmRig``; ``aloha`` is ``AlohaAdapter``, two
arms on the ``handover`` task pack; ``tidybot`` is ``TidybotAdapter``, a mobile manipulator on
the ``fetch`` task pack."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.sim.arm import rig_named
from uzumaki.sim.assets import ALOHA_MODEL, TIDYBOT_MODEL


class ArmAdapter:
    """Reachable cases and fresh physics worlds for the ``pick-place`` task pack on one arm."""

    def __init__(self, rig: str) -> None:
        self.rig = rig_named(rig)
        self.asset_dir = self.rig.assets.verify()

    def cases(self, task: str) -> tuple[FixtureCase, ...]:
        self._only_pick_place(task)
        return self.rig.cases()

    def environment(self, task: str) -> Callable[[FixtureCase], Any]:
        from uzumaki.sim.run import sim_environment

        self._only_pick_place(task)
        return sim_environment(self.asset_dir, rig=self.rig)

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None:
        """Joint and site names of the scene model (it adds the rig's site if the file has none)."""
        from uzumaki.sim.scene import build_model

        model = build_model(self.cases("pick-place")[0], self.asset_dir, self.rig)
        joints = frozenset(model.joint(i).name for i in range(model.njnt))
        sites = frozenset(model.site(i).name for i in range(model.nsite))
        return joints, sites

    def identity(self) -> dict[str, str]:
        from uzumaki.sim.run import simulator_identity

        return simulator_identity(self.rig)

    def _only_pick_place(self, task: str) -> None:
        if task != "pick-place":
            raise KeyError(
                f"the {self.rig.assets.label} adapter supports only the pick-place task, "
                f"not {task!r}"
            )


def SO101Adapter() -> ArmAdapter:  # noqa: N802 - the embodiment's adapter factory
    return ArmAdapter("so101")


def PandaAdapter() -> ArmAdapter:  # noqa: N802
    return ArmAdapter("panda")


class AlohaAdapter:
    """ALOHA's two arms (``uzumaki.sim.aloha``) for the ``handover`` task pack."""

    def __init__(self) -> None:
        from uzumaki.sim import aloha

        self.aloha = aloha
        self.asset_dir = ALOHA_MODEL.verify()

    def cases(self, task: str) -> tuple[FixtureCase, ...]:
        self._only_handover(task)
        return self.aloha.load_aloha_cases()

    def environment(self, task: str) -> Callable[[FixtureCase], Any]:
        self._only_handover(task)
        return self.aloha.aloha_environment(self.asset_dir)

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None:
        model = self.aloha.new_aloha_world(
            self.cases("handover")[0], FixtureClock(), self.asset_dir
        ).model
        joints = frozenset(model.joint(i).name for i in range(model.njnt))
        sites = frozenset(model.site(i).name for i in range(model.nsite))
        return joints, sites

    def identity(self) -> dict[str, str]:
        from uzumaki.sim.run import simulator_identity

        return simulator_identity(self.aloha.GIVER)

    @staticmethod
    def _only_handover(task: str) -> None:
        if task != "handover":
            raise KeyError(f"the ALOHA adapter supports only the handover task, not {task!r}")


class TidybotAdapter:
    """Stanford TidyBot (``uzumaki.sim.tidybot``) for the ``fetch`` task pack."""

    def __init__(self) -> None:
        from uzumaki.sim import tidybot

        self.tidybot = tidybot
        self.asset_dir = TIDYBOT_MODEL.verify()

    def cases(self, task: str) -> tuple[FixtureCase, ...]:
        self._only_fetch(task)
        return self.tidybot.load_tidybot_cases()

    def environment(self, task: str) -> Callable[[FixtureCase], Any]:
        self._only_fetch(task)
        return self.tidybot.tidybot_environment(self.asset_dir)

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None:
        model = self.tidybot.build_model(self.cases("fetch")[0], self.asset_dir)
        joints = frozenset(model.joint(i).name for i in range(model.njnt))
        sites = frozenset(model.site(i).name for i in range(model.nsite))
        return joints, sites

    def identity(self) -> dict[str, str]:
        from uzumaki.sim.run import simulator_identity

        return simulator_identity(self.tidybot.RIG)

    @staticmethod
    def _only_fetch(task: str) -> None:
        if task != "fetch":
            raise KeyError(f"the TidyBot adapter supports only the fetch task, not {task!r}")


def SpotAdapter() -> ArmAdapter:  # noqa: N802
    return ArmAdapter("spot")


def G1Adapter() -> ArmAdapter:  # noqa: N802
    return ArmAdapter("g1")
