"""Pinned SO-101 model from MuJoCo Menagerie, fetched on demand and verified by SHA-256.

The model is ``robotstudio_so101`` (Apache-2.0) at a fixed Menagerie commit. Files are never
committed to this repository: ``fetch`` downloads them into a cache directory and every load
verifies each file against ``FILES`` first, so a changed or partial download is refused instead of
silently simulating a different robot.

The cache is ``$UZUMAKI_SO101_ASSETS`` if set, otherwise ``~/.cache/uzumaki/so101-<commit>``.

Usage: ``python -m uzumaki.sim.assets fetch`` or ``python -m uzumaki.sim.assets verify``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from uzumaki.sim import SimUnavailable

MENAGERIE_COMMIT = "cda5236f98383f6fbaa4820c6b18dba892cd087f"
MODEL_DIR = "robotstudio_so101"
SOURCE_URL = (
    f"https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/{MENAGERIE_COMMIT}/"
    f"{MODEL_DIR}/"
)
ENV_VAR = "UZUMAKI_SO101_ASSETS"
MAX_FILE_BYTES = 8 << 20

#: Relative path -> SHA-256 of every file the scene loads, plus the model's license.
FILES: Mapping[str, str] = MappingProxyType(
    {
        "so101.xml": "982c02c027ec0c62d3459d0a6eb00dfb7dc13a451992e72ce99addc71f209805",
        "LICENSE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        "assets/base_motor_holder_so101_v1.stl": (
            "8cd2f241037ea377af1191fffe0dd9d9006beea6dcc48543660ed41647072424"
        ),
        "assets/base_so101_v2.stl": (
            "bb12b7026575e1f70ccc7240051f9d943553bf34e5128537de6cd86fae33924d"
        ),
        "assets/motor_holder_so101_base_v1.stl": (
            "31242ae6fb59d8b15c66617b88ad8e9bded62d57c35d11c0c43a70d2f4caa95b"
        ),
        "assets/motor_holder_so101_wrist_v1.stl": (
            "887f92e6013cb64ea3a1ab8675e92da1e0beacfd5e001f972523540545e08011"
        ),
        "assets/moving_jaw_so101_gripper_part0_v1.stl": (
            "3a92e115d1934d4c45cff808c4eff2ea10361a137a7f89940033a1f8a821b88c"
        ),
        "assets/moving_jaw_so101_gripper_part1_v1.stl": (
            "d10f6c671d26f62988ec95efee9370a0657b5833b3c5f8aad59fd69a87ef2aee"
        ),
        "assets/moving_jaw_so101_gripper_v1.stl": (
            "e8a5dc040b98e27453a5b74f9087d1dbca14589cf9a17c4de7868d5ab98413cd"
        ),
        "assets/moving_jaw_so101_v1.stl": (
            "785a9dded2f474bc1d869e0d3dae398a3dcd9c0c345640040472210d2861fa9d"
        ),
        "assets/rotation_pitch_so101_v1.stl": (
            "9be900cc2a2bf718102841ef82ef8d2873842427648092c8ed2ca1e2ef4ffa34"
        ),
        "assets/sts3215_03a_no_horn_v1.stl": (
            "75ef3781b752e4065891aea855e34dc161a38a549549cd0970cedd07eae6f887"
        ),
        "assets/sts3215_03a_v1.stl": (
            "a37c871fb502483ab96c256baf457d36f2e97afc9205313d9c5ab275ef941cd0"
        ),
        "assets/under_arm_so101_v1.stl": (
            "d01d1f2de365651dcad9d6669e94ff87ff7652b5bb2d10752a66a456a86dbc71"
        ),
        "assets/upper_arm_so101_v1.stl": (
            "475056e03a17e71919b82fd88ab9a0b898ab50164f2a7943652a6b2941bb2d4f"
        ),
        "assets/waveshare_mounting_plate_so101_v2.stl": (
            "e197e24005a07d01bbc06a8c42311664eaeda415bf859f68fa247884d0f1a6e9"
        ),
        "assets/wrist_roll_follower_so101_camera_mount.stl": (
            "58c457b33a26d9c9bb337aa351173e7123f84719a93f486541adef6eba57ae21"
        ),
        "assets/wrist_roll_follower_so101_gripper_part0_v1.stl": (
            "370eb470a6ab48b08febcaeb5274d2bcc91246692532ce12c795fb82977def72"
        ),
        "assets/wrist_roll_follower_so101_gripper_v1.stl": (
            "65f32debcf265420c0160afe004f8befc5df4b3e578fd59ebaa62eade0371224"
        ),
        "assets/wrist_roll_follower_so101_v1.stl": (
            "4b17b410a12d64ec39554abc3e8054d8a97384b2dc4a8d95a5ecb2a93670f5f4"
        ),
        "assets/wrist_roll_pitch_so101_v2.stl": (
            "6c7ec5525b4d8b9e397a30ab4bb0037156a5d5f38a4adf2c7d943d6c56eda5ae"
        ),
    }
)


def manifest_sha256() -> str:
    """One digest over the commit and every pinned file hash (recorded with sim evidence)."""
    lines = [MENAGERIE_COMMIT, *(f"{name} {digest}" for name, digest in sorted(FILES.items()))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def asset_dir() -> Path:
    """The configured asset cache directory (it may not exist yet)."""
    configured = os.environ.get(ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".cache" / "uzumaki" / f"so101-{MENAGERIE_COMMIT[:12]}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_assets(directory: Path | None = None) -> Path:
    """Return the asset directory if every pinned file is present and matches its hash.

    Raises ``SimUnavailable`` naming the first missing or mismatched file otherwise.
    """
    root = asset_dir() if directory is None else directory
    for name, digest in FILES.items():
        path = root / name
        if not path.is_file():
            raise SimUnavailable(
                f"SO-101 asset missing: {name} (run: python -m uzumaki.sim.assets fetch)"
            )
        if _sha256(path) != digest:
            raise SimUnavailable(f"SO-101 asset does not match its pinned hash: {name}")
    return root


def fetch_assets(directory: Path | None = None) -> Path:
    """Download every pinned file that is missing or wrong, verify it, then verify the set."""
    root = asset_dir() if directory is None else directory
    for name, digest in FILES.items():
        path = root / name
        if path.is_file() and _sha256(path) == digest:
            continue
        with urllib.request.urlopen(SOURCE_URL + name, timeout=60) as response:  # noqa: S310
            data = response.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise SimUnavailable(f"downloaded SO-101 asset does not match its pinned hash: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(data)
        os.replace(handle.name, path)
    return verify_assets(root)


def main(argv: list[str]) -> int:
    if argv not in (["fetch"], ["verify"]):
        print("usage: python -m uzumaki.sim.assets fetch|verify", file=sys.stderr)
        return 2
    try:
        root = fetch_assets() if argv == ["fetch"] else verify_assets()
    except SimUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    print(f"SO-101 assets verified in {root} (menagerie {MENAGERIE_COMMIT[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
