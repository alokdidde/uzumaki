"""The SO-101 environment adapter: the ``so101`` embodiment in MuJoCo (``uzumaki.embodiments``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import mujoco

from uzumaki.fixture_env import FixtureCase
from uzumaki.sim import assets
from uzumaki.sim.cases import load_so101_cases


class SO101Adapter:
    """Reachable SO-101 cases and fresh physics worlds for the ``pick-place`` task pack."""

    def __init__(self) -> None:
        self.asset_dir = assets.verify_assets()

    def cases(self, task: str) -> tuple[FixtureCase, ...]:
        _only_pick_place(task)
        return load_so101_cases()

    def environment(self, task: str) -> Callable[[FixtureCase], Any]:
        from uzumaki.sim.run import sim_environment

        _only_pick_place(task)
        return sim_environment(self.asset_dir)

    def model_names(self) -> tuple[frozenset[str], frozenset[str]] | None:
        model = mujoco.MjModel.from_xml_path(str(self.asset_dir / "so101.xml"))
        joints = frozenset(model.joint(i).name for i in range(model.njnt))
        sites = frozenset(model.site(i).name for i in range(model.nsite))
        return joints, sites

    def identity(self) -> dict[str, str]:
        from uzumaki.sim.run import simulator_identity

        return simulator_identity()


def _only_pick_place(task: str) -> None:
    if task != "pick-place":
        raise KeyError(f"the SO-101 adapter supports only the pick-place task, not {task!r}")
