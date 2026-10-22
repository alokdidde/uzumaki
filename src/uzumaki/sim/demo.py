"""The MVP loop end to end on the SO-101, from one command (features 17 and 18, slim).

    python -m uzumaki.sim.demo [--out DIR] [--case ID] [--provider NAME] [--model ID]
        [--rounds N] [--video] [--record FILE | --replay FILE]

1. **Before.** The model planner gets the task with a skill library that has no skill able to
   move the box (the hand-authored set without ``pick-box``, ``place-box`` and
   ``pick-and-place``). It should abstain; the evaluator records the outcome.
2. **Missing capability.** If the planner abstained, the generator writes ``transfer-box`` and
   repairs it from traces on the 16 development cases (``uzumaki.generator``).
3. **Confirmation.** The accepted program runs frozen on the 12 withheld cases, which include the
   task case and which the generator never saw.
4. **Promotion.** ``uzumaki.promotion.promote`` validates the skill with its provenance, and the
   library is saved to ``<out>/library``.
5. **After.** The library is loaded back from disk, and the same planner gets the same task again.
   It can now choose ``transfer-box``; the evaluator decides whether the box reached the target.

Writes ``before/``, ``round-<n>.py`` and ``round-<n>/``, ``confirmation/``, ``after/`` bundles,
the ``library/`` directory, ``summary.json`` and, with ``--video``, ``after.mp4``. Exits 0 only if
the after-run completed. The model is called live through any backend in ``uzumaki.backends``
unless ``--replay`` answers from recorded exchanges. Withheld cases are development cases kept
from repair, not a held-out set, so the result is a development demonstration, not acceptance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from uzumaki.backends import add_model_options, model_provider, save_exchanges
from uzumaki.baseline import MODEL_BUDGETS, CaseRun, SkillBody, export_evidence
from uzumaki.generator import (
    MAX_ROUNDS,
    TRANSFER_BOX,
    evaluate_program,
    generate_skill,
)
from uzumaki.planner import PROMPT_VERSION as PLANNER_PROMPT
from uzumaki.planner import ModelPlanner
from uzumaki.promotion import PromotionRefused, load_library, promote, save_library
from uzumaki.registry import SkillRegistry
from uzumaki.sim import SimUnavailable, assets
from uzumaki.sim.cases import load_so101_cases
from uzumaki.sim.generate import DEVELOPMENT_CASES, case_rows, load_registry, write_generation
from uzumaki.sim.run import DEFAULT_SKILLS, run_sim_case, sim_environment, simulator_identity

DEFAULT_CASE = "so101-dev-020"
#: Hand-authored skills removed from the starting library: together they are the capability.
REMOVED_SKILLS = ("pick-and-place", "pick-box", "place-box")


def starting_library(path: Path) -> SkillRegistry:
    """The hand-authored skill set without any skill that can move the box."""
    full = load_registry(path)
    snapshot = full.snapshot()
    registry = SkillRegistry(full.tools)
    for key in snapshot.keys():
        if key[0] in REMOVED_SKILLS:
            continue
        registry.register(snapshot.get(*key).document(), by="demo")
        if snapshot.state(*key) == "validated":
            registry.validate(*key, evidence_ref="hand-authored.review-1")
    return registry


def _planner_run(
    case: Any,
    registry: SkillRegistry,
    bodies: dict[str, SkillBody],
    provider: Any,
    model: str,
    asset_dir: Path,
    frame_hook: Any = None,
) -> CaseRun:
    snapshot = registry.snapshot()
    planner = ModelPlanner(snapshot, model, frozenset(bodies))
    run, _world = run_sim_case(
        case,
        snapshot,
        asset_dir=asset_dir,
        frame_hook=frame_hook,
        bodies=bodies,
        selector=planner,
        planner_id=planner.model_version,
        budgets=MODEL_BUDGETS,
        provider=provider,
    )
    return run


def _decisions(run: CaseRun) -> list[dict[str, Any]]:
    return [
        {
            key: event["data"]["output"].get(key)
            for key in ("action", "skill_id", "skill_version", "reason")
        }
        for event in run.bundle["events"]
        if event["kind"] == "decision"
    ]


def _outcome(run: CaseRun) -> dict[str, Any]:
    verdict = run.verdict or {}
    return {
        "case_id": run.case_id,
        "primary": verdict.get("primary"),
        "progress": verdict.get("progress"),
        "status": run.report.status,
        "violations": verdict.get("violations", 0),
        "model_calls": run.report.usage["model_calls"]["used"],
        "decisions": _decisions(run),
    }


def _primary(run: CaseRun) -> Any:
    return (run.verdict or {}).get("primary")


def _key(label: str) -> tuple[str, int]:
    skill_id, version = label.split("@")
    return skill_id, int(version)


def _save_run(out: Path, name: str, runs: list[CaseRun]) -> None:
    (out / name).mkdir()
    export_evidence(runs, out / name)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m uzumaki.sim.demo", description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("build/demo"))
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILLS)
    parser.add_argument("--case", default=DEFAULT_CASE, help="the task case (a withheld case)")
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--video", action="store_true", help="video of the after-run")
    add_model_options(parser)
    options = parser.parse_args(argv)
    cases = load_so101_cases()
    development, withheld = cases[:DEVELOPMENT_CASES], cases[DEVELOPMENT_CASES:]
    task = next((case for case in withheld if case.case_id == options.case), None)
    if task is None:
        print(
            f"--case must be a withheld case (so101-dev-017 to 028): {options.case}",
            file=sys.stderr,
        )
        return 2
    provider = model_provider(options)
    if provider is None:
        return 1
    try:
        asset_dir = assets.verify_assets()
    except SimUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    out: Path = options.out
    if out.exists() and any(out.iterdir()):
        print(
            f"output directory is not empty (evidence is never overwritten): {out}", file=sys.stderr
        )
        return 2
    out.mkdir(parents=True, exist_ok=True)
    environment = sim_environment(asset_dir)
    registry = starting_library(options.skills)
    summary: dict[str, Any] = {
        "identity": {
            **simulator_identity(),
            "model": options.model,
            "planner": PLANNER_PROMPT,
            "generator": "generator-v1",
        },
        "task": task.case_id,
    }
    try:
        before = _planner_run(task, registry, {}, provider, options.model, asset_dir)
        _save_run(out, "before", [before])
        summary["before"] = _outcome(before)
        action = _decisions(before)[-1]["action"]
        print(f"1. before: the planner's last decision was {action}; outcome {_primary(before)}")
        abstained = any(d["action"] == "abstain" for d in _decisions(before))
        if summary["before"]["primary"] == "completed" or not abstained:
            print("the planner did not report a missing capability; nothing to generate")
            summary["result"] = "no_missing_capability"
            return 1
        print("2. missing capability: generating transfer-box")
        report = generate_skill(
            TRANSFER_BOX,
            registry,
            development,
            environment,
            provider=provider,
            model=options.model,
            max_rounds=options.rounds,
        )
        write_generation(out, report)
        summary["generation"] = report.summary()
        for record in report.programs:
            passed = sum(r.passed for r in record.results)
            verdict = (
                "static checks failed"
                if record.static_rejections
                else f"{passed}/{len(record.results)} development cases"
            )
            print(f"   round {record.round}: {verdict}")
        accepted = report.accepted
        if accepted is None or accepted.skill is None:
            summary["result"] = f"generation_{report.status}"
            print(f"generation ended without an accepted program: {report.status}")
            return 1
        skill = registry.get(*_key(accepted.skill))
        confirmation = evaluate_program(
            registry.snapshot(), skill.key, accepted.source, withheld, environment
        )
        _save_run(out, "confirmation", [r.run for r in confirmation])
        summary["confirmation"] = case_rows(confirmation)
        print(
            f"3. confirmation (frozen, unseen cases): "
            f"{sum(r.passed for r in confirmation)}/{len(confirmation)}"
        )
        try:
            promotion = promote(registry, report, confirmation, by="uzumaki.sim.demo")
        except PromotionRefused as error:
            summary["result"] = f"promotion_refused:{error.reason}"
            print(f"promotion refused: {error.reason}")
            return 1
        library = save_library(
            out / "library", registry, {skill.label: accepted.source}, [promotion]
        )
        summary["promotion"] = {k: promotion[k] for k in ("skill", "skill_sha256", "evidence_ref")}
        print(f"4. promoted {skill.label} to validated; library in {library}")
        loaded, bodies = load_library(library)
        recorder = None
        if options.video:
            from uzumaki.sim.video import VideoRecorder

            recorder = VideoRecorder()
        after = _planner_run(
            task, loaded, dict(bodies), provider, options.model, asset_dir, recorder
        )
        _save_run(out, "after", [after])
        summary["after"] = _outcome(after)
        if recorder is not None:
            summary["after"]["video"] = recorder.write(out / "after.mp4").name
            recorder.close()
        primary = summary["after"]["primary"]
        print(f"5. after: planner chose {_decisions(after)[0].get('skill_id')}; outcome {primary}")
        completed = primary == "completed" and summary["after"]["violations"] == 0
        summary["result"] = "completed" if completed else "after_run_not_completed"
        return 0 if completed else 1
    finally:
        save_exchanges(options, provider)
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"demo {summary.get('result', 'failed')}; evidence in {out}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
