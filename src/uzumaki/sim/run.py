"""Run SO-101 cases through the unchanged harness: same driver, selector, skills and evidence.

``run_sim_case`` is ``uzumaki.baseline.run_case`` with a MuJoCo environment, so every bundle is a
normal ``uzumaki.events/1.0`` bundle and replays with ``uzumaki.baseline.replay_case``.

Command line (after ``pip install -r requirements-sim.txt`` and fetching assets)::

    python -m uzumaki.sim [--case ID ...] [--out DIR] [--video]
        [--planner rule|model] [--provider gateway|codex] [--model ID]
        [--record FILE | --replay FILE]

writes ``<case>.json`` bundles, optional ``<case>.mp4`` videos and ``summary.json`` with the
outcome, correctness failures and true final pose error of each case and the simulator identity.

``--planner model`` replaces the rule selector with ``uzumaki.planner.ModelPlanner``. By default
it calls the model live through ``uzumaki.gateway.GatewayProvider`` (needs network access to the
gateway and ``AI_GATEWAY_API_KEY``); ``--record FILE`` also keeps every exchange as a
``RecordedProvider`` file, and ``--replay FILE`` answers from such a file with no network.
``--provider codex`` calls the model through ``uzumaki.codex.CodexProvider`` instead: OpenAI's
Codex CLI, signed in with a ChatGPT account (``codex login``); ``--model`` then defaults to the
CLI's own model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import mujoco

from uzumaki.baseline import (
    BASELINE_BUDGETS,
    BASELINE_SKILL_BODIES,
    MODEL_BUDGETS,
    CaseEnvironment,
    CaseRun,
    SkillBody,
    correctness_failures,
    export_evidence,
    run_case,
)
from uzumaki.budgets import Budgets
from uzumaki.codex import CODEX_DEFAULT_MODEL, CodexProvider
from uzumaki.evidence import DecisionSource
from uzumaki.fixture_env import FixtureCase, FixtureClock
from uzumaki.gateway import DEFAULT_KEY_ENV, DEFAULT_MODEL, GatewayProvider, RecordingProvider
from uzumaki.planner import PROMPT_VERSION, ModelPlanner
from uzumaki.providers import RecordedProvider
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
    planner_id: str | None = None,
    budgets: Budgets = BASELINE_BUDGETS,
    provider: Any = None,
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

    run = run_case(
        case,
        snapshot,
        build,
        candidate=candidate,
        bodies=bodies,
        selector=selector,
        planner_id=planner_id,
        budgets=budgets,
        provider=provider,
    )
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
    parser.add_argument("--planner", choices=("rule", "model"), default="rule")
    parser.add_argument("--provider", choices=("gateway", "codex"), default="gateway")
    parser.add_argument("--model", help=f"model ID (gateway default: {DEFAULT_MODEL})")
    exchanges = parser.add_mutually_exclusive_group()
    exchanges.add_argument("--record", type=Path, help="keep model exchanges in this file")
    exchanges.add_argument("--replay", type=Path, help="answer from recorded model exchanges")
    options = parser.parse_args(argv)
    if options.model is None:
        options.model = CODEX_DEFAULT_MODEL if options.provider == "codex" else DEFAULT_MODEL
    provider: Any = None
    if options.planner == "model":
        if options.replay is not None:
            provider = RecordedProvider.from_path(options.replay)
        elif options.provider == "codex":
            provider = CodexProvider()
        else:
            if not os.environ.get(DEFAULT_KEY_ENV):
                print(
                    f"live model planner needs {DEFAULT_KEY_ENV} in the environment",
                    file=sys.stderr,
                )
                return 1
            provider = GatewayProvider()
        if options.record is not None:
            provider = RecordingProvider(provider)
    elif options.record or options.replay:
        parser.error("--record and --replay need --planner model")
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
        planner: ModelPlanner | None = None
        if provider is not None:
            planner = ModelPlanner(snapshot, options.model, frozenset(BASELINE_SKILL_BODIES))
        try:
            run, world = run_sim_case(
                case,
                snapshot,
                asset_dir=asset_dir,
                frame_hook=recorder,
                selector=planner,
                planner_id=None if planner is None else planner.model_version,
                budgets=BASELINE_BUDGETS if planner is None else MODEL_BUDGETS,
                provider=provider,
            )
        finally:
            if isinstance(provider, RecordingProvider) and options.record is not None:
                options.record.write_text(json.dumps(provider.document(), indent=1) + "\n")
        runs.append(run)
        failures = correctness_failures(case, run, model_calls=None if planner else 0)
        verdict = run.verdict or {}
        row = {
            "case_id": case.case_id,
            "primary": verdict.get("primary"),
            "progress": verdict.get("progress"),
            "status": run.report.status,
            "attempts": run.report.usage["attempts"]["used"],
            "model_calls": run.report.usage["model_calls"]["used"],
            "detail": run.report.detail,
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
    identity = simulator_identity()
    identity["planner"] = (
        "rule-selector-v1" if provider is None else f"{options.model}+{PROMPT_VERSION}"
    )
    summary = {"identity": identity, "cases": rows}
    (options.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    passed = sum(1 for row in rows if not row["correctness_failures"])
    print(f"{passed}/{len(rows)} cases met their frozen expectations; evidence in {options.out}")
    return 0 if passed == len(rows) else 1
