"""The SO-101 case set: ``pick-place-v1`` task rules on geometry the SO-101 can reach.

The frozen ``pick-place-v1`` case ranges (initial x in [-0.15, -0.05], target x in [0.05, 0.15],
y in [-0.15, 0.15]) span radii of about 0.05 to 0.40 m from any single base position. The SO-101
reaches top-down grasps between about 0.10 and 0.31 m from its base (0.15 to 0.26 m at the 0.08 m
carry height) with a +/-110 degree pan, so no base placement covers the frozen ranges. See
docs/decisions/0002-so101-simulation.md for the measurement.

This case set therefore keeps every task rule, tool contract, budget, completion threshold and
the SHA-256 seed recipe of ``pick-place-v1``, and changes only where boxes and targets are drawn:
in polar coordinates around the arm base at ``BASE_XY``, facing +y. ``generate_geometry`` is the
recipe; ``CASES_FILE`` is the frozen output, with eight fault cases that mirror the fixture
baseline cases and twenty nominal development cases. There is no SO-101 held-out set yet: it must
be generated and kept by the protocol custodian, never by a coding worker.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from uzumaki.fixture_env import FixtureCase, load_cases

CASES_FILE = Path(__file__).parent / "data" / "so101_cases_v1.json"
CASE_SET = "so101-dev-v1"
#: Arm base position on the tabletop (metres) and its heading (+y, degrees from +x).
BASE_XY = (0.0, -0.20)
BASE_YAW_DEG = 90.0
#: Seed recipe ranges: size m, radius m, bearing degrees from the base heading, yaw degrees.
RANGES = {
    "size": (0.03, 0.06),
    "initial_radius": (0.15, 0.23),
    "initial_bearing": (25.0, 60.0),
    "target_radius": (0.15, 0.23),
    "target_bearing": (25.0, 60.0),
    "yaw": (-45.0, 45.0),
}


def _unit(seed: str, label: str) -> float:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / (2**64 - 1)


def _draw(seed: str, label: str) -> float:
    low, high = RANGES[label]
    return round(low + (high - low) * _unit(seed, label), 6)


def generate_geometry(seed: str) -> dict[str, Any]:
    """Box size, initial pose and target for one seed (initial on the -x side, target on +x)."""

    def point(radius: float, bearing_deg: float, side: float) -> list[float]:
        bearing = math.radians(bearing_deg)
        return [
            round(BASE_XY[0] + side * radius * math.sin(bearing), 6),
            round(BASE_XY[1] + radius * math.cos(bearing), 6),
        ]

    return {
        "box_size_m": _draw(seed, "size"),
        "initial_xy_m": point(_draw(seed, "initial_radius"), _draw(seed, "initial_bearing"), -1),
        "initial_yaw_deg": _draw(seed, "yaw"),
        "target_xy_m": point(_draw(seed, "target_radius"), _draw(seed, "target_bearing"), 1),
    }


def load_so101_cases(path: Path = CASES_FILE) -> tuple[FixtureCase, ...]:
    """The frozen SO-101 case set (``uzumaki.baseline-cases/1.0``)."""
    return load_cases(path)
