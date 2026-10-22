"""Promote a generated skill with provenance, keep it in a skill library, roll it back (feature 17,
first cut).

``promote`` moves an accepted ``candidate`` to ``validated`` only when the evidence supports it:
the generation session accepted the program on every development case, and the frozen program
then passed every confirmation case, none of which was a development case, with each run's bundle
naming this exact skill version as its candidate. The promotion record keeps the provenance: skill
and program digests, prompt and model, the whole candidate lineage and the SHA-256 of every
development and confirmation bundle. The registry transition carries the record's
``evidence_ref``.

A skill library is a directory::

    skills.json        uzumaki.skillset/1.0: every skill with its state and evidence_ref
    programs/<id>@<v>.py   the program of each validated or deprecated generated skill
    promotions.json    uzumaki.promotions/1.0: promotion and rollback records, in order

``load_library`` rebuilds the registry and a ``SandboxedProgram`` body for every validated program,
after checking each file against the digest in its skill's instructions and promotion record.
``rollback`` deprecates a validated version (it stays in the library for provenance and is no
longer eligible) and appends a rollback record. Nothing here is undone silently: libraries are
append-only apart from state changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from uzumaki.generator import (
    PROMPT_VERSION,
    CaseResult,
    GenerationReport,
    SandboxedProgram,
    WorkerFactory,
    program_sha256,
    restricted_worker,
)
from uzumaki.registry import SKILL_SET_FORMAT, SkillRegistry, canonical_json, load_skill_set

PROMOTIONS_FORMAT = "uzumaki.promotions/1.0"
_PROGRAM_DIGEST = re.compile(r"sha256 ([0-9a-f]{64})\.")


class PromotionRefused(Exception):
    """The evidence does not support promotion. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def bundle_sha256(result: CaseResult) -> str:
    return hashlib.sha256(canonical_json(result.run.bundle)).hexdigest()


def promote(
    registry: SkillRegistry,
    report: GenerationReport,
    confirmation: Iterable[CaseResult],
    *,
    by: str = "harness",
) -> dict[str, Any]:
    """Validate the session's accepted program; returns the promotion record."""
    accepted = report.accepted
    if accepted is None or accepted.skill is None:
        raise PromotionRefused("not_accepted")
    confirmed = list(confirmation)
    if not confirmed:
        raise PromotionRefused("no_confirmation")
    skill_id, version_text = accepted.skill.split("@")
    version = int(version_text)
    candidate_id = f"{skill_id}:{version}"
    development = {r.case_id for r in accepted.results}
    if any(r.case_id in development for r in confirmed):
        raise PromotionRefused("confirmation_overlaps_development")
    for result in [*accepted.results, *confirmed]:
        if result.run.bundle["manifest"].get("candidate_id") != candidate_id:
            raise PromotionRefused("evidence_for_another_skill")
    if not all(r.passed for r in confirmed):
        raise PromotionRefused("confirmation_failed")
    if registry.state(skill_id, version) != "candidate":
        raise PromotionRefused("not_a_candidate")
    if registry.get(skill_id, version).sha256 != accepted.skill_sha256:
        raise PromotionRefused("skill_changed")
    evidence_ref = f"promotion.{skill_id}.{version}"
    registry.validate(skill_id, version, evidence_ref=evidence_ref)
    return {
        "action": "promoted",
        "skill": accepted.skill,
        "skill_sha256": accepted.skill_sha256,
        "program_sha256": accepted.program_sha256,
        "prompt_version": PROMPT_VERSION,
        "model": accepted.model,
        "evidence_ref": evidence_ref,
        "by": by,
        "lineage": [record.summary() for record in report.programs],
        "development": {r.case_id: bundle_sha256(r) for r in accepted.results},
        "confirmation": {r.case_id: bundle_sha256(r) for r in confirmed},
    }


# ------------------------------------------------------------------------------ library
def _evidence_refs(registry: SkillRegistry) -> dict[tuple[str, int], str | None]:
    refs: dict[tuple[str, int], str | None] = {}
    for event in registry.history():
        refs[(event.skill_id, event.skill_version)] = event.evidence_ref
    return refs


def _program_path(directory: Path, label: str) -> Path:
    return directory / "programs" / f"{label}.py"


def _read_records(directory: Path) -> list[dict[str, Any]]:
    path = directory / "promotions.json"
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format") != PROMOTIONS_FORMAT:
        raise ValueError("unsupported promotions file")
    records: list[dict[str, Any]] = document["records"]
    return records


def _write_skills(directory: Path, registry: SkillRegistry) -> None:
    snapshot = registry.snapshot()
    refs = _evidence_refs(registry)
    entries = []
    # Registration order, so every dependency precedes its dependents when loaded again.
    registered = [
        (e.skill_id, e.skill_version) for e in registry.history() if e.action == "registered"
    ]
    for key in registered:
        state = snapshot.state(*key)
        entries.append(
            {
                "artifact": snapshot.get(*key).document(),
                "state": state,
                "evidence_ref": None if state == "candidate" else refs.get(key),
            }
        )
    document = {"format": SKILL_SET_FORMAT, "skills": entries}
    (directory / "skills.json").write_text(json.dumps(document, indent=1) + "\n", "utf-8")


def save_library(
    directory: str | Path,
    registry: SkillRegistry,
    programs: Mapping[str, str],
    records: Iterable[Mapping[str, Any]] = (),
) -> Path:
    """Write the registry, the given programs (``label -> source``) and new records."""
    target = Path(directory)
    (target / "programs").mkdir(parents=True, exist_ok=True)
    _write_skills(target, registry)
    new_records = [dict(record) for record in records]
    digests = {
        r["skill"]: r["program_sha256"] for r in new_records if r.get("action") == "promoted"
    }
    for label, source in programs.items():
        if label in digests and digests[label] != program_sha256(source):
            raise ValueError(f"program does not match its promotion record: {label}")
        path = _program_path(target, label)
        if path.exists() and path.read_text(encoding="utf-8") != source:
            raise ValueError("a different program is already stored for this skill version")
        path.write_text(source, encoding="utf-8")
    all_records = _read_records(target) + new_records
    document = {"format": PROMOTIONS_FORMAT, "records": all_records}
    (target / "promotions.json").write_text(json.dumps(document, indent=1) + "\n", "utf-8")
    return target


def load_library(
    directory: str | Path, worker: WorkerFactory = restricted_worker
) -> tuple[SkillRegistry, dict[str, SandboxedProgram]]:
    """The library's registry and a sandboxed body for every validated generated program.

    Raises ``ValueError`` when a program file is missing or does not match the digest bound in
    its skill and recorded at promotion."""
    target = Path(directory)
    registry = load_skill_set(target / "skills.json")
    promoted = {
        r["skill"]: r["program_sha256"]
        for r in _read_records(target)
        if r.get("action") == "promoted"
    }
    snapshot = registry.snapshot()
    bodies: dict[str, SandboxedProgram] = {}
    for key in snapshot.keys():
        skill = snapshot.get(*key)
        if snapshot.state(*key) != "validated" or skill.label not in promoted:
            continue
        path = _program_path(target, skill.label)
        if not path.exists():
            raise ValueError(f"program missing for {skill.label}")
        source = path.read_text(encoding="utf-8")
        bound = _PROGRAM_DIGEST.search(skill.instructions)
        digest = program_sha256(source)
        if bound is None or bound.group(1) != digest or promoted[skill.label] != digest:
            raise ValueError(f"program does not match its promoted digest: {skill.label}")
        bodies[skill.label] = SandboxedProgram(source, worker)
    return registry, bodies


def rollback(directory: str | Path, label: str, *, reason: str, by: str = "harness") -> int:
    """Deprecate a validated library skill; returns the new registry version."""
    target = Path(directory)
    registry = load_skill_set(target / "skills.json")
    skill_id, version_text = label.split("@")
    evidence_ref = f"rollback.{skill_id}.{version_text}"
    version = registry.deprecate(skill_id, int(version_text), evidence_ref=evidence_ref)
    _write_skills(target, registry)
    record = {"action": "rolled_back", "skill": label, "reason": reason, "by": by}
    document = {"format": PROMOTIONS_FORMAT, "records": _read_records(target) + [record]}
    (target / "promotions.json").write_text(json.dumps(document, indent=1) + "\n", "utf-8")
    return version
