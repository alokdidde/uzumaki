"""Versioned skill registry: immutable hashed artifacts, states, pinning and integrity checks."""

import copy
import dataclasses
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from uzumaki import providers
from uzumaki.broker import PICK_PLACE_V1_TOOLS, pick_place_v1_broker
from uzumaki.budgets import Budgets
from uzumaki.registry import (
    MAX_ELIGIBLE,
    PICK_PLACE_V1_OUTCOMES,
    REASONS,
    Dependency,
    SkillError,
    SkillRegistry,
    canonical_json,
    load_skill,
    load_skill_set,
    parse_json,
    seal_skill,
    skill_digest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "skills_pick_place_v1.json"
CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "v1.json"
DELETE = object()


def rejects(reason: str, action: Callable[[], object]) -> SkillError:
    with pytest.raises(SkillError) as caught:
        action()
    assert caught.value.reason == reason
    assert caught.value.reason in REASONS
    return caught.value


def fixture_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["skills"]
    return entries


def body(skill_id: str = "pick-box", **changes: Any) -> dict[str, Any]:
    for entry in fixture_entries():
        if entry["artifact"]["skill_id"] == skill_id:
            document = copy.deepcopy(entry["artifact"])
            document.update(changes)
            return document
    raise KeyError(skill_id)


def sealed(skill_id: str = "pick-box", **changes: Any) -> dict[str, Any]:
    return seal_skill(body(skill_id, **changes))


def write_set(tmp_path: Path, entries: list[dict[str, Any]], name: str = "set.json") -> Path:
    data = {
        "format": "uzumaki.skillset/1.0",
        "skills": [dict(entry, artifact=seal_skill(entry["artifact"])) for entry in entries],
    }
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def loaded(tmp_path: Path) -> SkillRegistry:
    return load_skill_set(write_set(tmp_path, fixture_entries()))


def mutate(document: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    target = document
    for key in path[:-1]:
        target = target[key]
    if value is DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


def eligible_schema() -> dict[str, Any]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    schema: dict[str, Any] = contract["records"]["eligible_skills"]
    return schema


# --- constants tied to the frozen protocol and contract ------------------------------------


def test_outcomes_match_the_contract_lifecycle_primaries() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    primaries = set().union(*contract["lifecycle"]["lifecycle_to_primary"].values())
    assert set(PICK_PLACE_V1_OUTCOMES) == primaries
    assert len(PICK_PLACE_V1_OUTCOMES) == 15


def noop(args: dict[str, Any]) -> dict[str, Any]:
    return {}


def test_default_tool_catalog_is_the_pick_place_v1_broker_manifest() -> None:
    handlers = {name: noop for name in PICK_PLACE_V1_TOOLS}
    assert SkillRegistry().tools == pick_place_v1_broker(handlers).manifest()["tools"]


# --- hand-authored skill set, offline ------------------------------------------------------


def test_hand_authored_skill_set_loads_without_any_model_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_provider(*args: object, **kwargs: object) -> None:
        raise AssertionError("model provider used while loading skills")

    monkeypatch.setattr(providers.RecordedProvider, "__init__", no_provider)
    registry = loaded(tmp_path)
    snapshot = registry.snapshot()
    assert snapshot.keys() == (
        ("observe-scene", 1),
        ("pick-and-place", 1),
        ("pick-box", 1),
        ("place-box", 1),
        ("push-box", 1),
    )
    assert snapshot.state("pick-box", 1) == "validated"
    assert snapshot.state("push-box", 1) == "candidate"
    # Five registrations and four validations, each a new registry version after version 1.
    assert registry.registry_version == 10
    history = registry.history()
    assert [event.registry_version for event in history] == list(range(2, 11))
    assert {event.by for event in history} == {"skill_set"}
    refs = [event.evidence_ref for event in history if event.action == "transition"]
    assert refs == ["hand-authored.review-1"] * 4
    registry.verify()


def test_repository_fixture_is_unsealed_and_rejected_as_is() -> None:
    # The committed fixture holds hand-authored bodies; digests are added by seal_skill.
    error = rejects("malformed", lambda: load_skill_set(FIXTURE))
    assert error.subject == "skills[0]"


def test_every_declared_field_is_stored_and_digest_bound(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    skill = registry.get("pick-box", 1)
    source = body("pick-box")
    assert skill.instructions == source["instructions"]
    assert skill.inputs == source["inputs"]
    assert skill.outputs == source["outputs"]
    assert skill.applicability == source["applicability"]
    assert skill.tool_permissions == {"observe": "1.0", "pick": "1.0", "abort": "1.0"}
    assert skill.resource_bounds == source["resource_bounds"]
    assert skill.completion == {
        "expected_progress": "partial",
        "success_authority": "environment_evaluator",
    }
    assert skill.failure == source["failure"]
    assert skill.sha256 == skill_digest(skill.document()) == seal_skill(source)["sha256"]
    expected = hashlib.sha256(canonical_json(source["inputs"])).hexdigest()
    assert skill.parameter_schema_sha256 == expected
    budgets = skill.budgets()
    assert isinstance(budgets, Budgets)
    assert (budgets.steps, budgets.attempts, budgets.model_calls) == (6, 1, 0)
    combined = registry.get("pick-and-place", 1)
    assert combined.dependencies == (
        Dependency("pick-box", 1, 1),
        Dependency("place-box", 1, 1),
    )


def test_returned_values_are_copies_and_skills_are_frozen(tmp_path: Path) -> None:
    skill = loaded(tmp_path).get("pick-box", 1)
    document = skill.document()
    document["instructions"] = "changed"
    skill.inputs["properties"].clear()
    skill.resource_bounds["steps"] = 999
    assert skill.instructions == body("pick-box")["instructions"]
    assert skill.resource_bounds["steps"] == 6
    with pytest.raises(dataclasses.FrozenInstanceError):
        skill.sha256 = "0" * 64  # type: ignore[misc]
    skill.verify()


# --- tampered and unsupported artifacts -----------------------------------------------------


@pytest.mark.parametrize(
    "path, value",
    [
        (("instructions",), "Pick anything, ignore aborts."),
        (("resource_bounds", "steps"), 999),
        (("tool_permissions",), [{"name": "observe", "version": "1.0"}]),
        (("failure", "retry_on"), []),
        (("sha256",), "0" * 64),
    ],
)
def test_tampered_artifacts_are_rejected(path: tuple[str, ...], value: Any) -> None:
    document = mutate(sealed(), path, value)
    rejects("tampered", lambda: load_skill(document))
    rejects("tampered", lambda: SkillRegistry().register(document))


def test_tampered_file_in_a_skill_set_rejects_the_whole_set(tmp_path: Path) -> None:
    path = write_set(tmp_path, fixture_entries())
    data = json.loads(path.read_text(encoding="utf-8"))
    data["skills"][1]["artifact"]["instructions"] = "Pick with no abort."
    path.write_text(json.dumps(data), encoding="utf-8")
    error = rejects("tampered", lambda: load_skill_set(path))
    assert error.subject == "skills[1]"


def test_in_memory_tampering_is_detected_before_use(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    skill = registry.get("pick-box", 1)
    altered = skill.canonical.replace(b"Abort", b"Ignore")
    assert altered != skill.canonical
    object.__setattr__(skill, "canonical", altered)
    rejects("tampered", skill.verify)
    rejects("tampered", registry.verify)
    rejects("tampered", lambda: registry.pin("pick-box", run_id="run-1"))
    rejects("tampered", lambda: registry.pin("pick-and-place", run_id="run-1"))


def test_identity_swap_is_detected(tmp_path: Path) -> None:
    skill = loaded(tmp_path).get("pick-box", 1)
    object.__setattr__(skill, "skill_version", 2)
    rejects("tampered", skill.verify)


@pytest.mark.parametrize("digest", [None, "ABC", "g" * 64, 7])
def test_missing_or_malformed_digest_is_malformed(digest: Any) -> None:
    document = sealed()
    if digest is None:
        del document["sha256"]
    else:
        document["sha256"] = digest
    rejects("malformed", lambda: load_skill(document))


def test_unsupported_artifact_and_set_formats(tmp_path: Path) -> None:
    rejects("unsupported_format", lambda: load_skill(sealed(format="uzumaki.skill/2.0")))
    rejects("unsupported_format", lambda: load_skill(sealed(format=None)))
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"format": "uzumaki.skillset/2.0", "skills": []}))
    rejects("unsupported_format", lambda: load_skill_set(path))


def test_oversized_artifact_and_set(tmp_path: Path) -> None:
    rejects("oversized", lambda: load_skill(sealed(instructions="x" * 70000)))
    path = tmp_path / "big.json"
    path.write_bytes(b" " * ((1 << 20) + 1))
    rejects("oversized", lambda: load_skill_set(path))


@pytest.mark.parametrize(
    "data",
    [
        b'{"format": "uzumaki.skillset/1.0", "format": "x", "skills": []}',
        b'{"format": "uzumaki.skillset/1.0", "skills": [NaN]}',
        b"\xff\xfe",
        b"[1, 2",
    ],
)
def test_strict_json_parsing(data: bytes) -> None:
    rejects("malformed", lambda: parse_json(data))


# --- invalid contracts and malformed fields -------------------------------------------------


@pytest.mark.parametrize(
    "path, value",
    [
        (("inputs", "additionalProperties"), True),
        (("inputs", "required"), ["grasp_height_m", "missing_field"]),
        (("inputs", "type"), "array"),
        (("inputs", "properties", "Bad"), {"type": "boolean"}),
        (("inputs", "properties", "note"), {"type": "string"}),
        (("inputs", "properties", "note"), {"type": "string", "maxLength": 129}),
        (("inputs", "properties", "note"), {"type": "string", "maxLength": 3, "enum": ["long"]}),
        (("inputs", "properties", "h"), {"type": "number", "minimum": 1, "maximum": 0}),
        (("inputs", "properties", "h"), {"type": "integer", "minimum": 0.5}),
        (("inputs", "properties", "h"), {"type": "number", "minimum": True}),
        (("inputs", "properties", "h"), {"type": "object"}),
        (("inputs", "properties", "h"), {"type": ["number"]}),
        (("inputs", "properties", "h"), {"type": "boolean", "default": True}),
        (("outputs", "required"), ["held", "held"]),
        (("outputs",), {"type": "object"}),
        (("resource_bounds", "attempts"), 4),
        (("resource_bounds", "model_calls"), 21),
        (("resource_bounds", "steps"), 0),
        (("resource_bounds", "steps"), True),
        (("resource_bounds", "wall_ms"), DELETE),
        (("resource_bounds", "max_output_bytes"), 20000),
        (("completion", "success_authority"), "planner"),
        (("completion", "expected_progress"), "unknown"),
        (("failure", "retry_on"), ["invariant_violation"]),
        (("failure", "retry_on"), ["collision"]),
        (("failure", "stop_on"), ["completed"]),
        (("failure", "stop_on"), ["misgrasp"]),
        (("failure", "stop_on"), ["dropped_box"]),
        (("failure", "otherwise"), "retry_forever"),
    ],
)
def test_invalid_contracts_are_rejected(path: tuple[str, ...], value: Any) -> None:
    document = seal_skill(mutate(body(), path, value))
    rejects("invalid_contract", lambda: load_skill(document))


def test_a_valid_contract_extension_is_accepted() -> None:
    document = body()
    document["inputs"]["properties"]["tries"] = {"type": "integer", "minimum": 0, "maximum": 3}
    document["outputs"]["properties"]["note"] = {"type": "string", "maxLength": 8, "enum": ["ok"]}
    assert load_skill(seal_skill(document)).inputs["properties"]["tries"]["maximum"] == 3


@pytest.mark.parametrize(
    "path, value",
    [
        (("skill_id",), "Pick Box"),
        (("skill_version",), 0),
        (("skill_version",), True),
        (("skill_version",), "1"),
        (("instructions",), ""),
        (("instructions",), "x" * 8193),
        (("applicability", "summary"), "x" * 513),
        (("applicability", "task_protocols"), []),
        (("applicability", "required_observations"), ["pose", "pose"]),
        (("tool_permissions",), []),
        (("tool_permissions",), [{"name": "pick"}]),
        (("tool_permissions",), [{"name": "pick", "version": "1"}]),
        (("tool_permissions",), [{"name": "pick", "version": "1.0"}] * 2),
        (("dependencies",), [{"skill_id": "place-box", "min_version": 2, "max_version": 1}]),
        (("dependencies",), [{"skill_id": "place-box", "min_version": 1}]),
        (("unexpected",), 1),
        (("failure",), DELETE),
    ],
)
def test_malformed_fields_are_rejected(path: tuple[str, ...], value: Any) -> None:
    document = seal_skill(mutate(body(), path, value))
    rejects("malformed", lambda: load_skill(document))


def test_non_object_artifact_is_malformed() -> None:
    rejects("malformed", lambda: load_skill([]))


# --- missing tools --------------------------------------------------------------------------


def test_undeclared_tool_is_rejected() -> None:
    tools = [{"name": "observe", "version": "1.0"}, {"name": "grasp", "version": "1.0"}]
    rejects("unknown_tool", lambda: SkillRegistry().register(sealed(tool_permissions=tools)))


def test_tool_missing_from_the_registry_catalog_is_rejected(tmp_path: Path) -> None:
    catalog = {"observe": "1.0", "pick": "1.0", "abort": "1.0"}
    rejects("unknown_tool", lambda: SkillRegistry(catalog).register(sealed("place-box")))
    path = write_set(tmp_path, fixture_entries())
    error = rejects("unknown_tool", lambda: load_skill_set(path, tools=catalog))
    assert error.subject == "skills[2]"


def test_registry_built_from_a_partial_broker_manifest_rejects_other_tools() -> None:
    broker = pick_place_v1_broker({"observe": noop})
    registry = SkillRegistry(broker.manifest()["tools"])
    registry.register(sealed("observe-scene"))
    rejects("unknown_tool", lambda: registry.register(sealed("pick-box")))


def test_tool_version_mismatch_is_rejected() -> None:
    tools = [{"name": "observe", "version": "2.0"}]
    rejects(
        "tool_version_mismatch",
        lambda: SkillRegistry().register(sealed("observe-scene", tool_permissions=tools)),
    )


def test_invalid_tool_catalog_is_refused() -> None:
    with pytest.raises(ValueError):
        SkillRegistry({"Pick": "1.0"})
    with pytest.raises(ValueError):
        SkillRegistry({"pick": "v1"})


# --- dependencies and incompatible versions -------------------------------------------------


def depends(*deps: tuple[str, int, int]) -> list[dict[str, Any]]:
    return [{"skill_id": s, "min_version": low, "max_version": high} for s, low, high in deps]


def base_registry() -> SkillRegistry:
    registry = SkillRegistry()
    for skill_id in ("pick-box", "place-box"):
        registry.register(sealed(skill_id))
        registry.validate(skill_id, 1, evidence_ref="review-1")
    return registry


def test_missing_dependency_is_rejected() -> None:
    document = sealed("pick-and-place", dependencies=depends(("lift-box", 1, 1)))
    rejects("missing_dependency", lambda: base_registry().register(document))


def test_dependency_version_outside_the_range_is_incompatible() -> None:
    document = sealed("pick-and-place", dependencies=depends(("pick-box", 2, 3)))
    rejects("incompatible_dependency", lambda: base_registry().register(document))


def test_dependency_on_a_rejected_version_is_incompatible() -> None:
    registry = SkillRegistry()
    registry.register(sealed("pick-box"))
    registry.register(sealed("place-box"))
    registry.reject("pick-box", 1, evidence_ref="review-1")
    rejects("incompatible_dependency", lambda: registry.register(sealed("pick-and-place")))


def test_dependency_needing_more_tools_than_granted_is_incompatible() -> None:
    document = sealed("observe-scene", dependencies=depends(("pick-box", 1, 1)))
    rejects("incompatible_dependency", lambda: base_registry().register(document))


def test_self_dependency_and_cycles_are_rejected() -> None:
    rejects(
        "dependency_cycle",
        lambda: load_skill(sealed("pick-box", dependencies=depends(("pick-box", 1, 1)))),
    )
    registry = SkillRegistry()
    registry.register(sealed("observe-scene", skill_id="skill-a"))
    registry.register(
        sealed("observe-scene", skill_id="skill-b", dependencies=depends(("skill-a", 1, 5)))
    )
    cyclic = sealed(
        "observe-scene",
        skill_id="skill-a",
        skill_version=2,
        dependencies=depends(("skill-b", 1, 1)),
    )
    rejects("dependency_cycle", lambda: registry.register(cyclic))


def test_validation_requires_validated_dependencies() -> None:
    registry = SkillRegistry()
    for skill_id in ("pick-box", "place-box", "pick-and-place"):
        registry.register(sealed(skill_id))
    rejects(
        "incompatible_dependency",
        lambda: registry.validate("pick-and-place", 1, evidence_ref="review-1"),
    )
    assert registry.state("pick-and-place", 1) == "candidate"


# --- duplicate and conflicting IDs ----------------------------------------------------------


def test_duplicate_registration_is_rejected_without_changing_the_registry() -> None:
    registry = SkillRegistry()
    registry.register(sealed())
    version = registry.registry_version
    rejects("duplicate_skill", lambda: registry.register(sealed()))
    assert registry.registry_version == version


def test_same_id_and_version_with_different_content_conflicts() -> None:
    registry = SkillRegistry()
    registry.register(sealed())
    rejects("conflicting_skill", lambda: registry.register(sealed(instructions="Other.")))
    assert registry.get("pick-box", 1).instructions == body()["instructions"]


def test_versions_of_one_id_must_increase() -> None:
    registry = SkillRegistry()
    registry.register(sealed(skill_version=2))
    rejects("conflicting_skill", lambda: registry.register(sealed(skill_version=1)))


def test_duplicate_entries_reject_a_skill_set(tmp_path: Path) -> None:
    entries = fixture_entries()
    entries.append(copy.deepcopy(entries[1]))
    error = rejects("duplicate_skill", lambda: load_skill_set(write_set(tmp_path, entries)))
    assert error.subject == "skills[5]"


@pytest.mark.parametrize(
    "entry",
    [
        {"state": "promoted", "evidence_ref": "r-1"},
        {"state": "validated", "evidence_ref": None},
        {"state": "candidate", "evidence_ref": "r-1"},
        {"state": "validated", "evidence_ref": "Not An ID"},
    ],
)
def test_malformed_skill_set_entries(tmp_path: Path, entry: dict[str, Any]) -> None:
    entries = fixture_entries()
    entries[0].update(entry)
    rejects("malformed", lambda: load_skill_set(write_set(tmp_path, entries)))


# --- states ---------------------------------------------------------------------------------


def test_states_are_separate_and_only_validated_skills_are_eligible() -> None:
    registry = SkillRegistry()
    for skill_id in ("observe-scene", "pick-box", "place-box", "push-box"):
        registry.register(sealed(skill_id))
    assert registry.snapshot().eligible() == ()
    rejects("not_validated", lambda: registry.pin("pick-box", run_id="run-1"))
    registry.validate("observe-scene", 1, evidence_ref="review-1")
    registry.validate("pick-box", 1, evidence_ref="review-1")
    registry.deprecate("pick-box", 1, evidence_ref="review-2")
    registry.reject("push-box", 1, evidence_ref="review-3")
    states = {key: registry.state(*key) for key in registry.snapshot().keys()}
    assert states == {
        ("observe-scene", 1): "validated",
        ("pick-box", 1): "deprecated",
        ("place-box", 1): "candidate",
        ("push-box", 1): "rejected",
    }
    eligible = [skill.key for skill in registry.snapshot().eligible()]
    assert eligible == [("observe-scene", 1)]
    for skill_id in ("pick-box", "place-box", "push-box"):
        rejects("not_validated", lambda s=skill_id: registry.pin(s, 1, run_id="run-1"))


@pytest.mark.parametrize(
    "setup, target",
    [
        ((), "deprecated"),
        (("rejected",), "validated"),
        (("validated",), "rejected"),
        (("validated",), "candidate"),
        (("validated", "deprecated"), "validated"),
        (("validated",), "validated"),
        ((), "promoted"),
    ],
)
def test_disallowed_transitions_change_nothing(setup: tuple[str, ...], target: str) -> None:
    registry = SkillRegistry()
    registry.register(sealed("observe-scene"))
    for step in setup:
        registry.transition("observe-scene", 1, step, evidence_ref="review-1")
    before = (registry.registry_version, registry.state("observe-scene", 1))
    rejects(
        "invalid_transition",
        lambda: registry.transition("observe-scene", 1, target, evidence_ref="review-2"),
    )
    assert (registry.registry_version, registry.state("observe-scene", 1)) == before


def test_transitions_need_a_known_skill_and_an_evidence_reference() -> None:
    registry = SkillRegistry()
    registry.register(sealed("observe-scene"))
    rejects("unknown_skill", lambda: registry.validate("observe-scene", 2, evidence_ref="r-1"))
    rejects("malformed", lambda: registry.validate("observe-scene", 1, evidence_ref=""))
    rejects("unknown_skill", lambda: registry.pin("lift-box", run_id="run-1"))
    rejects("malformed", lambda: registry.pin("observe-scene", run_id="Run 1"))


def test_history_records_every_change_with_its_evidence_reference() -> None:
    registry = SkillRegistry()
    skill = registry.register(sealed("observe-scene"))
    registry.validate("observe-scene", 1, evidence_ref="review-1")
    registry.deprecate("observe-scene", 1, evidence_ref="review-2")
    rows = [
        (e.registry_version, e.action, e.from_state, e.to_state, e.evidence_ref, e.sha256)
        for e in registry.history()
    ]
    assert rows == [
        (2, "registered", None, "candidate", None, skill.sha256),
        (3, "transition", "candidate", "validated", "review-1", skill.sha256),
        (4, "transition", "validated", "deprecated", "review-2", skill.sha256),
    ]


# --- pinning --------------------------------------------------------------------------------


def test_registry_changes_cannot_alter_a_pinned_execution(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    old_snapshot = registry.snapshot()
    pinned = registry.pin("pick-and-place", run_id="run-1")
    document = pinned.skill.document()
    dependencies = [(d.key, d.sha256, d.canonical) for d in pinned.dependencies]
    assert [key for key, _, _ in dependencies] == [("pick-box", 1), ("place-box", 1)]

    pick_v2 = body(
        "pick-box", skill_version=2, instructions="Pick with a slower approach, then abort."
    )
    pick_v2["inputs"]["properties"]["grasp_height_m"]["maximum"] = 0.5
    registry.register(seal_skill(pick_v2))
    registry.validate("pick-box", 2, evidence_ref="review-2")
    registry.deprecate("pick-box", 1, evidence_ref="review-2")
    registry.deprecate("pick-and-place", 1, evidence_ref="review-2")
    combined_v2 = body("pick-and-place", skill_version=2)
    combined_v2["dependencies"] = depends(("pick-box", 1, 2), ("place-box", 1, 1))
    registry.register(seal_skill(combined_v2))
    registry.validate("pick-and-place", 2, evidence_ref="review-2")
    registry.reject("push-box", 1, evidence_ref="review-2")

    assert pinned.registry_version == 10
    assert (pinned.skill_id, pinned.skill_version) == ("pick-and-place", 1)
    assert pinned.skill.document() == document
    assert [(d.key, d.sha256, d.canonical) for d in pinned.dependencies] == dependencies
    assert pinned.skill.budgets() == registry.get("pick-and-place", 1).budgets()
    pinned.skill.check_parameters({"grasp_height_m": 0.2})
    rejects("invalid_parameters", lambda: pinned.skill.check_parameters({"grasp_height_m": 0.3}))
    pinned.verify()

    assert old_snapshot.registry_version == 10
    assert old_snapshot.state("pick-and-place", 1) == "validated"
    assert old_snapshot.keys() == (
        ("observe-scene", 1),
        ("pick-and-place", 1),
        ("pick-box", 1),
        ("place-box", 1),
        ("push-box", 1),
    )
    assert len(registry.snapshot().keys()) == 7
    assert old_snapshot.pin("pick-and-place", run_id="run-2").sha256 == pinned.sha256

    fresh = registry.pin("pick-and-place", run_id="run-3")
    assert fresh.skill_version == 2
    assert [d.key for d in fresh.dependencies] == [("pick-box", 2), ("place-box", 1)]
    rejects("not_validated", lambda: registry.pin("pick-and-place", 1, run_id="run-3"))


def test_deprecating_a_dependency_blocks_new_pins_but_not_existing_ones(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    pinned = registry.pin("pick-and-place", run_id="run-1")
    registry.deprecate("place-box", 1, evidence_ref="review-2")
    rejects("incompatible_dependency", lambda: registry.pin("pick-and-place", run_id="run-2"))
    assert "pick-and-place" not in [s.skill_id for s in registry.snapshot().eligible()]
    assert [d.key for d in pinned.dependencies] == [("pick-box", 1), ("place-box", 1)]
    pinned.verify()


# --- eligible view (contract 1.0 eligible_skills) -------------------------------------------


def test_eligible_view_matches_the_contract_schema(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    record = registry.snapshot().eligible_skills(
        eligible_set_id="es-1",
        based_on_observation_id="obs-1",
        computed_at_ms=10,
        task_protocol="pick-place-v1",
    )
    jsonschema.Draft202012Validator(eligible_schema()).validate(record)
    payload = record["payload"]
    assert payload["registry_version"] == registry.registry_version
    assert payload["access"] == "read_only"
    skills = {entry["skill_id"]: entry for entry in payload["skills"]}
    assert list(skills) == ["observe-scene", "pick-and-place", "pick-box", "place-box"]
    pick = registry.get("pick-box", 1)
    assert skills["pick-box"] == {
        "skill_id": "pick-box",
        "skill_version": 1,
        "content_sha256": pick.sha256,
        "parameter_schema_sha256": pick.parameter_schema_sha256,
        "tool_permissions": ["observe", "pick", "abort"],
        "applicability": pick.applicability["summary"],
    }
    other = registry.snapshot().eligible_skills(
        eligible_set_id="es-2",
        based_on_observation_id="obs-1",
        computed_at_ms=10,
        task_protocol="stack-v1",
    )
    assert other["payload"]["skills"] == []


def test_eligible_view_lists_only_the_latest_validated_version(tmp_path: Path) -> None:
    registry = loaded(tmp_path)
    registry.register(sealed("observe-scene", skill_version=2))
    assert ("observe-scene", 1) in [s.key for s in registry.snapshot().eligible()]
    registry.validate("observe-scene", 2, evidence_ref="review-2")
    keys = [s.key for s in registry.snapshot().eligible()]
    assert ("observe-scene", 2) in keys and ("observe-scene", 1) not in keys


def test_eligible_view_is_bounded_and_checks_its_identifiers() -> None:
    registry = SkillRegistry()
    for index in range(MAX_ELIGIBLE + 1):
        skill_id = f"observe-{index:02d}"
        registry.register(sealed("observe-scene", skill_id=skill_id))
        registry.validate(skill_id, 1, evidence_ref="review-1")
    snapshot = registry.snapshot()
    rejects(
        "too_many_eligible",
        lambda: snapshot.eligible_skills(
            eligible_set_id="es-1", based_on_observation_id="obs-1", computed_at_ms=0
        ),
    )
    rejects(
        "malformed",
        lambda: snapshot.eligible_skills(
            eligible_set_id="Bad ID", based_on_observation_id="obs-1", computed_at_ms=0
        ),
    )
    rejects(
        "malformed",
        lambda: snapshot.eligible_skills(
            eligible_set_id="es-1", based_on_observation_id="obs-1", computed_at_ms=-1
        ),
    )


# --- input and output contracts -------------------------------------------------------------


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"grasp_height_m": 0.3},
        {"grasp_height_m": -0.1},
        {"grasp_height_m": True},
        {"grasp_height_m": "0.1"},
        {"grasp_height_m": 0.1, "speed": 1},
        [("grasp_height_m", 0.1)],
    ],
)
def test_parameters_outside_the_input_contract_are_rejected(parameters: Any) -> None:
    skill = load_skill(sealed())
    rejects("invalid_parameters", lambda: skill.check_parameters(parameters))


def test_parameters_and_results_inside_the_contracts_are_accepted() -> None:
    pick = load_skill(sealed())
    pick.check_parameters({"grasp_height_m": 0})
    pick.check_parameters({"grasp_height_m": 0.2})
    pick.check_result({"held": True})
    rejects("invalid_parameters", lambda: pick.check_result({"held": 1}))
    place = load_skill(sealed("place-box"))
    place.check_parameters({})
    place.check_parameters({"approach": "vertical"})
    rejects("invalid_parameters", lambda: place.check_parameters({"approach": "sideways"}))
