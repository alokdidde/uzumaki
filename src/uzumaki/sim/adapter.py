"""Environment adapters for the simulated arms: the ``so101`` and ``panda`` embodiments in MuJoCo
(``uzumaki.embodiments``). Both are one ``ArmAdapter`` over the arm's ``ArmRig``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uzumaki.fixture_env import FixtureCase
from uzumaki.sim.arm import rig_named


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
