"""Promotion with provenance, the skill library and rollback (feature 17, first cut).

Generation needs the restricted worker, so this module skips on unadmitted platforms (and fails
under ``UZUMAKI_REQUIRE_CONTAINMENT=1``, ``make containment``).
"""

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from generated_programs import STALE_PLACE, TRANSFER, ScriptedGenerator
from uzumaki.baseline import fixture_environment, run_case
from uzumaki.fixture_env import load_cases
from uzumaki.generator import TRANSFER_BOX, evaluate_program, generate_skill
from uzumaki.promotion import (
    PromotionRefused,
    load_library,
    promote,
    rollback,
    save_library,
)
from uzumaki.registry import SkillRegistry, load_skill_set, seal_skill
from uzumaki.sandbox import ContainmentUnavailable, admit, probe_platform

FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "baseline_cases_v1.json")
DEVELOPMENT = [
    c for c in CASES if c.case_id not in ("retry-exhausted", "cancellation", "worker-crash")
]
CONFIRMATION = [c for c in CASES if c not in DEVELOPMENT]


def _admission() -> str | None:
    try:
        admit(probe_platform())
    except ContainmentUnavailable as error:
        return str(error)
    return None


UNSUPPORTED = _admission()
if UNSUPPORTED is not None and os.environ.get("UZUMAKI_REQUIRE_CONTAINMENT") == "1":
    raise RuntimeError(f"containment required but platform not admitted: {UNSUPPORTED}")

pytestmark = pytest.mark.skipif(
    UNSUPPORTED is not None, reason=f"unverified platform: {UNSUPPORTED}"
)


def registry(tmp_path: Path) -> SkillRegistry:
    document = json.loads((FIXTURES / "skills_pick_place_v1.json").read_text(encoding="utf-8"))
    document["skills"] = [
        dict(entry, artifact=seal_skill(entry["artifact"])) for entry in document["skills"]
    ]
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_skill_set(path)


@pytest.fixture
def session(tmp_path: Path) -> tuple[SkillRegistry, Any, list[Any]]:
    reg = registry(tmp_path)
    report = generate_skill(
        TRANSFER_BOX,
        reg,
        DEVELOPMENT,
        fixture_environment,
        provider=ScriptedGenerator([STALE_PLACE, TRANSFER]),
        model="test-model",
    )
    assert report.status == "accepted"
    confirmation = evaluate_program(
        reg.snapshot(), ("transfer-box", 2), TRANSFER, CONFIRMATION, fixture_environment
    )
    return reg, report, confirmation


def test_promotion_validates_with_full_provenance(session: Any) -> None:
    reg, report, confirmation = session
    record = promote(reg, report, confirmation)
    assert reg.state("transfer-box", 2) == "validated"
    assert reg.state("transfer-box", 1) == "rejected"
    event = reg.history()[-1]
    assert (event.to_state, event.evidence_ref) == ("validated", "promotion.transfer-box.2")
    assert record["skill"] == "transfer-box@2"
    assert record["program_sha256"] == report.accepted.program_sha256
    assert record["skill_sha256"] == reg.get("transfer-box", 2).sha256
    assert [step["skill"] for step in record["lineage"]] == ["transfer-box@1", "transfer-box@2"]
    assert sorted(record["development"]) == sorted(c.case_id for c in DEVELOPMENT)
    assert sorted(record["confirmation"]) == sorted(c.case_id for c in CONFIRMATION)
    assert all(len(digest) == 64 for digest in record["confirmation"].values())


def test_promotion_is_refused_without_supporting_evidence(session: Any) -> None:
    reg, report, confirmation = session
    with pytest.raises(PromotionRefused, match="no_confirmation"):
        promote(reg, report, [])
    overlap = evaluate_program(
        reg.snapshot(), ("transfer-box", 2), TRANSFER, DEVELOPMENT[:1], fixture_environment
    )
    with pytest.raises(PromotionRefused, match="confirmation_overlaps_development"):
        promote(reg, report, confirmation + overlap)
    failed = [dataclasses.replace(confirmation[0], passed=False), *confirmation[1:]]
    with pytest.raises(PromotionRefused, match="confirmation_failed"):
        promote(reg, report, failed)
    other = run_case(CONFIRMATION[0], reg.snapshot(), fixture_environment)  # rule baseline run
    foreign = [dataclasses.replace(confirmation[0], run=other), *confirmation[1:]]
    with pytest.raises(PromotionRefused, match="evidence_for_another_skill"):
        promote(reg, report, foreign)
    rejected = dataclasses.replace(report, programs=report.programs[:1])
    with pytest.raises(PromotionRefused, match="not_accepted"):
        promote(reg, rejected, confirmation)
    assert reg.state("transfer-box", 2) == "candidate"
    promote(reg, report, confirmation)
    with pytest.raises(PromotionRefused, match="not_a_candidate"):
        promote(reg, report, confirmation)


def test_library_round_trip_runs_the_promoted_skill(session: Any, tmp_path: Path) -> None:
    reg, report, confirmation = session
    record = promote(reg, report, confirmation)
    library = save_library(
        tmp_path / "library", reg, {"transfer-box@2": report.accepted.source}, [record]
    )
    loaded, bodies = load_library(library)
    snapshot = loaded.snapshot()
    assert snapshot.state("transfer-box", 2) == "validated"
    assert snapshot.state("transfer-box", 1) == "rejected"
    assert snapshot.get("transfer-box", 2).sha256 == record["skill_sha256"]
    assert list(bodies) == ["transfer-box@2"]
    assert "transfer-box" in [s.skill_id for s in snapshot.eligible("pick-place-v1")]
    promotions = json.loads((library / "promotions.json").read_text(encoding="utf-8"))
    assert [r["action"] for r in promotions["records"]] == ["promoted"]


def test_tampered_or_missing_programs_are_refused(session: Any, tmp_path: Path) -> None:
    reg, report, confirmation = session
    record = promote(reg, report, confirmation)
    with pytest.raises(ValueError, match="promotion record"):
        save_library(tmp_path / "other", reg, {"transfer-box@2": TRANSFER + "#"}, [record])
    library = save_library(
        tmp_path / "library", reg, {"transfer-box@2": report.accepted.source}, [record]
    )
    with pytest.raises(ValueError, match="different program"):
        save_library(library, reg, {"transfer-box@2": TRANSFER + "\n# changed\n"})
    program = library / "programs" / "transfer-box@2.py"
    program.write_text(report.accepted.source.replace("vertical", "angled"), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_library(library)
    program.unlink()
    with pytest.raises(ValueError, match="missing"):
        load_library(library)


def test_rollback_deprecates_and_keeps_provenance(session: Any, tmp_path: Path) -> None:
    reg, report, confirmation = session
    record = promote(reg, report, confirmation)
    library = save_library(
        tmp_path / "library", reg, {"transfer-box@2": report.accepted.source}, [record]
    )
    rollback(library, "transfer-box@2", reason="regression found in review")
    loaded, bodies = load_library(library)
    snapshot = loaded.snapshot()
    assert snapshot.state("transfer-box", 2) == "deprecated"
    assert "transfer-box" not in [s.skill_id for s in snapshot.eligible("pick-place-v1")]
    assert bodies == {}
    assert (library / "programs" / "transfer-box@2.py").exists()
    promotions = json.loads((library / "promotions.json").read_text(encoding="utf-8"))
    assert [r["action"] for r in promotions["records"]] == ["promoted", "rolled_back"]
    with pytest.raises(Exception):  # noqa: B017 - deprecated is final
        rollback(library, "transfer-box@2", reason="again")
