"""Run SO-101 cases through the unchanged harness: same driver, selector, skills and evidence.

``run_sim_case`` is ``uzumaki.baseline.run_case`` with a MuJoCo environment, so every bundle is a
normal ``uzumaki.events/1.0`` bundle and replays with ``uzumaki.baseline.replay_case``.

Command line (after ``pip install -r requirements-sim.txt`` and fetching assets)::

    python -m uzumaki.sim [--case ID ...] [--out DIR] [--video]

writes ``<case>.json`` bundles, optional ``<case>.mp4`` videos and ``summary.json`` with the
outcome, correctness failures and true final pose error of each case and the simulator identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import mujoco

from uzumaki.baseline import (
    CaseEnvironment,
    CaseRun,
    SkillBody,
    correctness_failures,
    export_evidence,
    run_case,
)
from uzumaki.evidence import DecisionSource
from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.registry import RegistrySnapshot, load_skill_set, seal_skill
from uzumaki.sim import SimUnavailable, assets
from uzumaki.sim.cases import CASE_SET, CASES_FILE, load_so101_cases
from uzumaki.sim.world import FrameHook, SimEvaluator, SimWorld, new_world

DEFAULT_SKILLS = Path("tests/fixtures/skills_pick_place_v1.json")


def simulator_identity() -> dict[str, str]:
    """Versions and hashes that pin a simulated run (recorded in ``summary.json``)."""
    return {
        "simulator": f"mujoco-{mujoco.__version__}",
        "robot": f"menagerie/{assets.MODEL_DIR}@{assets.MENAGERIE_COMMIT}",
        "assets_manifest_sha256": assets.manifest_sha256(),
        "case_set": CASE_SET,
        "cases_sha256": hashlib.sha256(CASES_FILE.read_bytes()).hexdigest(),
    }


def sim_environment(
    asset_dir: Path, frame_hook: FrameHook | None = None
) -> Callable[[FixtureCase], CaseEnvironment]:
    """Environment factory for ``run_case``: a fresh physics world and evaluator per case."""

    def build(case: FixtureCase) -> CaseEnvironment:
        clock = FixtureClock()
        world = new_world(case, clock, asset_dir, frame_hook)
        return CaseEnvironment(clock, world, SimEvaluator(world))

    return build


def run_sim_case(
    case: FixtureCase,
    snapshot: RegistrySnapshot,
    *,
    asset_dir: Path | None = None,
    frame_hook: FrameHook | None = None,
    candidate: tuple[str, int] | None = None,
    bodies: Mapping[str, SkillBody] | None = None,
    selector: DecisionSource | None = None,
) -> tuple[CaseRun, SimWorld]:
    """Run one case in simulation; also returns the world for its true final state."""
    root = assets.verify_assets(asset_dir)
    worlds: list[SimWorld] = []
    factory = sim_environment(root, frame_hook)

    def build(case: FixtureCase) -> CaseEnvironment:
        env = factory(case)
        assert isinstance(env.world, SimWorld)
        worlds.append(env.world)
        return env

    run = run_case(case, snapshot, build, candidate=candidate, bodies=bodies, selector=selector)
    return run, worlds[0]


def load_snapshot(path: Path) -> RegistrySnapshot:
    """A registry snapshot of a hand-authored ``uzumaki.skillset/1.0`` file, sealing each
    artifact first (the fixture skill files carry unsealed artifacts)."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    with tempfile.TemporaryDirectory() as directory:
        sealed = Path(directory) / "skills.json"
        sealed.write_text(json.dumps(document), encoding="utf-8")
        return load_skill_set(sealed).snapshot()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m uzumaki.sim", description=__doc__)
    parser.add_argument("--case", action="append", help="case ID to run (default: all)")
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILLS)
    parser.add_argument("--out", type=Path, default=Path("build/sim"))
    parser.add_argument("--video", action="store_true", help="also write <case>.mp4")
    options = parser.parse_args(argv)
    try:
        asset_dir = assets.verify_assets()
    except SimUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    cases = load_so101_cases()
    if options.case:
        unknown = set(options.case) - {case.case_id for case in cases}
        if unknown:
            print(f"unknown case: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        cases = tuple(case for case in cases if case.case_id in options.case)
    if options.out.exists() and any(options.out.iterdir()):
        print(
            f"output directory is not empty (evidence is never overwritten): {options.out}",
            file=sys.stderr,
        )
        return 2
    snapshot = load_snapshot(options.skills)
    options.out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    runs: list[CaseRun] = []
    for case in cases:
        recorder = None
        if options.video:
            from uzumaki.sim.video import VideoRecorder

            recorder = VideoRecorder()
        started = time.monotonic()
        run, world = run_sim_case(case, snapshot, asset_dir=asset_dir, frame_hook=recorder)
        runs.append(run)
        failures = correctness_failures(case, run)
        verdict = run.verdict or {}
        row = {
            "case_id": case.case_id,
            "primary": verdict.get("primary"),
            "progress": verdict.get("progress"),
            "status": run.report.status,
            "attempts": run.report.usage["attempts"]["used"],
            "violations": verdict.get("violations"),
            "contacts": run.audit.get("contacts", []),
            "correctness_failures": failures,
            "final": world.final_state(),
            "wall_s": round(time.monotonic() - started, 2),
        }
        if recorder is not None:
            row["video"] = recorder.write(options.out / f"{case.case_id}.mp4").name
            recorder.close()
        rows.append(row)
        mark = "ok  " if not failures else "FAIL"
        print(
            f"{mark} {case.case_id:24} {row['primary']:20} attempts={row['attempts']} "
            f"xy_err={row['final']['xy_error_m']:.4f} yaw_err={row['final']['yaw_error_deg']:.1f}"
        )
    digests = export_evidence(runs, options.out)
    for row in rows:
        row["bundle_sha256"] = digests[row["case_id"]]
    summary = {"identity": simulator_identity(), "cases": rows}
    (options.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    passed = sum(1 for row in rows if not row["correctness_failures"])
    print(f"{passed}/{len(rows)} cases met their frozen expectations; evidence in {options.out}")
    return 0 if passed == len(rows) else 1
