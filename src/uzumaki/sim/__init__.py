"""MuJoCo simulation of ``pick-place-v1`` on a Robot Studio SO-101 arm.

This package is optional. It needs the pinned simulator requirements
(``python -m pip install -r requirements-sim.txt``) and the pinned SO-101 assets
(``python -m uzumaki.sim.assets fetch``). Nothing else in ``uzumaki`` imports it, so ``make check``
stays offline and dependency-free.

- ``uzumaki.sim.assets``: the pinned MuJoCo Menagerie SO-101 model, fetched and SHA-256 verified.
- ``uzumaki.sim.cases``: the SO-101 case set (reachable geometry, same task rules).
- ``uzumaki.sim.scene``: table, box, target marker and the arm placed at the table edge.
- ``uzumaki.sim.so101``: inverse kinematics and grasp planning for top-down grasps.
- ``uzumaki.sim.world``: the physics world behind the four declared tools and its evaluator.
- ``uzumaki.sim.run``: runs cases through the unchanged harness and writes evidence and video.
"""

from __future__ import annotations


class SimUnavailable(RuntimeError):
    """The simulator dependencies or the pinned assets are missing or do not verify."""
