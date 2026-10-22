"""Generate and repair a missing SO-101 skill with the model, then confirm it on unseen cases.

Command line (after ``pip install -r requirements-sim.txt`` and fetching assets)::

    python -m uzumaki.sim.generate [--out DIR] [--provider NAME] [--model ID]
        [--rounds N] [--video] [--record FILE | --replay FILE]

The generator (``uzumaki.generator``, capability ``transfer-box``) writes a program; each checked
program runs sandboxed on the 16 development cases (the eight fault mirrors and so101-dev-009 to
016), and failures go back as trace feedback for at most ``--rounds`` rounds. An accepted program
is then run frozen, with no feedback, on the 12 confirmation cases so101-dev-017 to 028, which the
generator never sees. These are development cases withheld from repair, not a held-out set.

Writes ``round-<n>.py`` (each program), ``round-<n>/<case>.json`` bundles, ``confirmation/``
bundles, ``summary.json`` and, with ``--video``, ``accepted-<case>.mp4`` of the accepted program
on the first confirmation case. Calls the model live through any backend in
``uzumaki.backends`` (``--provider``, ``--model``, ``--base-url``), unless ``--replay`` answers
from a recorded exchanges file.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from uzumaki.backends import add_model_options, model_provider, save_exchanges
from uzumaki.baseline import export_evidence
from uzumaki.generator import (
    MAX_ROUNDS,
    TRANSFER_BOX,
    CaseResult,
    GenerationReport,
    evaluate_program,
    generate_skill,
)
from uzumaki.registry import SkillRegistry, load_skill_set, seal_skill
from uzumaki.sim import SimUnavailable, assets
from uzumaki.sim.cases import load_so101_cases
from uzumaki.sim.run import DEFAULT_SKILLS, sim_environment, simulator_identity

#: Development cases (repair feedback) and confirmation cases (frozen run, never in feedback).
DEVELOPMENT_CASES = 16


def load_registry(path: Path) -> SkillRegistry:
    """A mutable registry of a hand-authored skill set (artifacts sealed first)."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    with tempfile.TemporaryDirectory() as directory:
        sealed = Path(directory) / "skills.json"
        sealed.write_text(json.dumps(document), encoding="utf-8")
        return load_skill_set(sealed)


def case_rows(results: list[CaseResult]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": r.case_id,
            "passed": r.passed,
            "failed_stage": r.failed_stage,
            "candidate_verdict": r.verdict,
            **r.observed,
        }
        for r in results
    ]


def write_generation(out: Path, report: GenerationReport) -> None:
    for record in report.programs:
        (out / f"round-{record.round}.py").write_text(record.source, encoding="utf-8")
        if record.results:
            directory = out / f"round-{record.round}"
            directory.mkdir()
            export_evidence([r.run for r in record.results], directory)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m uzumaki.sim.generate", description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("build/generate"))
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILLS)
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--video", action="store_true", help="video of the accepted program")
    add_model_options(parser)
    options = parser.parse_args(argv)
    provider = model_provider(options)
    if provider is None:
        return 1
    try:
        asset_dir = assets.verify_assets()
    except SimUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    if options.out.exists() and any(options.out.iterdir()):
        print(
            f"output directory is not empty (evidence is never overwritten): {options.out}",
            file=sys.stderr,
        )
        return 2
    options.out.mkdir(parents=True, exist_ok=True)
    cases = load_so101_cases()
    development, confirmation = cases[:DEVELOPMENT_CASES], cases[DEVELOPMENT_CASES:]
    registry = load_registry(options.skills)
    environment = sim_environment(asset_dir)
    try:
        report = generate_skill(
            TRANSFER_BOX,
            registry,
            development,
            environment,
            provider=provider,
            model=options.model,
            max_rounds=options.rounds,
        )
    finally:
        save_exchanges(options, provider)
    write_generation(options.out, report)
    summary: dict[str, Any] = {
        "identity": {**simulator_identity(), "generator": f"{options.model}+generator-v1"},
        "generation": report.summary(),
        "development": {
            f"round-{p.round}": case_rows(p.results) for p in report.programs if p.results
        },
    }
    for record in report.programs:
        verdict = (
            "static checks failed"
            if record.static_rejections
            else (
                f"{sum(r.passed for r in record.results)}/{len(record.results)} development cases"
            )
        )
        print(f"round {record.round}: {record.skill or 'not registered'}: {verdict}")
    accepted = report.accepted
    code = 1
    if accepted is not None and accepted.skill is not None:
        skill_id, version = accepted.skill.split("@")
        key = (skill_id, int(version))
        results = evaluate_program(
            registry.snapshot(), key, accepted.source, confirmation, environment
        )
        (options.out / "confirmation").mkdir()
        export_evidence([r.run for r in results], options.out / "confirmation")
        summary["confirmation"] = case_rows(results)
        passed = sum(r.passed for r in results)
        print(f"confirmation (frozen, no feedback): {passed}/{len(results)} cases")
        if options.video:
            from uzumaki.sim.video import VideoRecorder

            recorder = VideoRecorder()
            filmed = evaluate_program(
                registry.snapshot(),
                key,
                accepted.source,
                confirmation[:1],
                sim_environment(asset_dir, recorder),
            )
            summary["video"] = recorder.write(
                options.out / f"accepted-{filmed[0].case_id}.mp4"
            ).name
            recorder.close()
        code = 0 if passed == len(results) else 1
    (options.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"generation {report.status}; evidence in {options.out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
