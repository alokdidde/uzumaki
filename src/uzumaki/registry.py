"""Versioned, content-hashed skill registry (issue 07).

A skill is an immutable artifact (``uzumaki.skill/1.0``): a closed JSON document holding its ID,
integer version, instructions, input and output contracts, applicability, tool permissions,
resource bounds, completion and failure semantics, and dependencies. Its ``sha256`` is the SHA-256
of the canonical JSON of the document without the ``sha256`` key. A document whose digest does not
match is ``tampered`` and is never registered. The digest is an integrity check, not a signature.

Lifecycle state (``candidate``, ``validated``, ``deprecated``, ``rejected``) is registry metadata,
never part of the artifact, so changing state cannot change a skill. Every registration or state
change creates a new integer ``registry_version``. ``snapshot()`` returns an immutable view of one
version, and ``pin()`` resolves a validated skill and its dependency closure into a self-contained
``PinnedSkill`` for one run. Later registry changes cannot alter a pin or an earlier snapshot.

Rejections raise ``SkillError`` with a fixed ``reason`` code from ``REASONS``; messages never echo
document content. The registry only stores and checks artifacts. It executes nothing, calls no
model provider, and its state changes record a caller-supplied evidence reference without
verifying that evidence (promotion with evidence checks is feature 17).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from uzumaki.broker import PICK_PLACE_V1_TOOL_VERSION, PICK_PLACE_V1_TOOLS
from uzumaki.budgets import Budgets

SKILL_FORMAT = "uzumaki.skill/1.0"
SKILL_SET_FORMAT = "uzumaki.skillset/1.0"
REGISTRY_PROTOCOL = "uzumaki.registry/1.0"
CONTRACT_VERSION = "1.0"

STATES = ("candidate", "validated", "deprecated", "rejected")
#: Allowed state changes. ``deprecated`` and ``rejected`` are final here; rollback is feature 17.
TRANSITIONS = frozenset(
    {("candidate", "validated"), ("candidate", "rejected"), ("validated", "deprecated")}
)

#: The 15 ``pick-place-v1`` primary outcomes (contract 1.0 ``lifecycle.lifecycle_to_primary``).
PICK_PLACE_V1_OUTCOMES = (
    "abstained",
    "ambiguous_goal",
    "budget_exhausted",
    "cancelled",
    "collision",
    "completed",
    "evaluation_failure",
    "invariant_violation",
    "misgrasp",
    "missing_observation",
    "partial_progress",
    "placement_failure",
    "timeout",
    "tool_rejected",
    "unreachable_target",
)
#: Outcomes a skill may never retry on its own: they end the case, report a violation or crash,
#: or mean the budget or time is gone. ``completed`` is not a failure at all.
NEVER_RETRY = frozenset(
    {
        "abstained",
        "budget_exhausted",
        "cancelled",
        "collision",
        "completed",
        "evaluation_failure",
        "invariant_violation",
        "timeout",
    }
)
PROGRESS = ("none", "partial", "goal_reached")

REASONS = (
    "malformed",
    "oversized",
    "unsupported_format",
    "tampered",
    "invalid_contract",
    "unknown_tool",
    "tool_version_mismatch",
    "duplicate_skill",
    "conflicting_skill",
    "missing_dependency",
    "incompatible_dependency",
    "dependency_cycle",
    "unknown_skill",
    "invalid_transition",
    "not_validated",
    "invalid_parameters",
    "too_many_eligible",
)

#: Contract 1.0 ``limits.max_input_bytes``, applied to the canonical artifact.
MAX_ARTIFACT_BYTES = 65536
MAX_SKILL_SET_BYTES = 1 << 20
MAX_SKILLS = 256
MAX_INSTRUCTIONS = 8192
MAX_APPLICABILITY = 512
MAX_DEPENDENCIES = 8
#: Contract 1.0 ``eligible_skills.payload.skills.maxItems``.
MAX_ELIGIBLE = 32
MAX_CONTRACT_FIELDS = 16
MAX_STRING_LENGTH = 128
MAX_ENUM = 32
MAX_DESCRIPTION = 256

#: Upper bounds for ``resource_bounds``. ``attempts`` and ``model_calls`` are the protocol budget
#: (no fourth attempt, no model call 21); the others are registry ceilings, not measurements.
RESOURCE_CEILINGS: Mapping[str, int] = MappingProxyType(
    {
        "wall_ms": 3_600_000,
        "model_calls": 20,
        "steps": 1000,
        "retries": 10,
        "attempts": 3,
        "max_output_bytes": 65536,
        "max_total_output_bytes": 1 << 20,
    }
)
_MAY_BE_ZERO = frozenset({"model_calls", "retries"})

_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TOOL_VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,31}")
_OBSERVATION = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_SAFE_INT = 9007199254740991

_ARTIFACT_KEYS = frozenset(
    {
        "format",
        "skill_id",
        "skill_version",
        "sha256",
        "instructions",
        "applicability",
        "inputs",
        "outputs",
        "tool_permissions",
        "resource_bounds",
        "completion",
        "failure",
        "dependencies",
    }
)
_FIELD_KEYS = {
    "number": frozenset({"type", "description", "minimum", "maximum"}),
    "integer": frozenset({"type", "description", "minimum", "maximum"}),
    "string": frozenset({"type", "description", "maxLength", "enum"}),
    "boolean": frozenset({"type", "description"}),
}


class SkillError(ValueError):
    """A skill, skill set or registry request was rejected. ``reason`` is in ``REASONS``."""

    def __init__(self, reason: str, subject: str | None = None) -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(reason if subject is None else f"{reason}: {subject}")


def canonical_json(value: Any) -> bytes:
    """Sorted keys, ``,``/``:`` separators, ASCII escapes; NaN and Infinity are refused."""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise SkillError("malformed") from None
    return text.encode("ascii")


def skill_digest(document: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical document without its ``sha256`` key."""
    body = {key: value for key, value in document.items() if key != "sha256"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def seal_skill(body: Mapping[str, Any]) -> dict[str, Any]:
    """Authoring helper: return a copy of ``body`` with its ``sha256`` set.

    Sealing does not validate anything; ``load_skill`` and ``SkillRegistry.register`` do.
    """
    if not isinstance(body, Mapping):
        raise SkillError("malformed")
    document = json.loads(canonical_json(dict(body)))
    document["sha256"] = skill_digest(document)
    return dict(document)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return (type(value) is int or type(value) is float) and math.isfinite(value)


def _closed(value: Any, keys: frozenset[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SkillError(reason)
    return value


def _unique_strings(
    value: Any, pattern: re.Pattern[str], low: int, high: int, reason: str
) -> list[str]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise SkillError(reason)
    if not all(isinstance(item, str) and pattern.fullmatch(item) for item in value):
        raise SkillError(reason)
    if len(set(value)) != len(value):
        raise SkillError(reason)
    return value


def _check_field(spec: Any) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        raise SkillError("invalid_contract")
    if spec["type"] not in _FIELD_KEYS:
        raise SkillError("invalid_contract")
    kind = spec["type"]
    if not set(spec) <= _FIELD_KEYS[kind]:
        raise SkillError("invalid_contract")
    description = spec.get("description")
    if description is not None and (
        not isinstance(description, str) or not 1 <= len(description) <= MAX_DESCRIPTION
    ):
        raise SkillError("invalid_contract")
    if kind in ("number", "integer"):
        check = _is_int if kind == "integer" else _is_number
        bounds = [spec[key] for key in ("minimum", "maximum") if key in spec]
        if not all(check(bound) for bound in bounds):
            raise SkillError("invalid_contract")
        if len(bounds) == 2 and bounds[0] > bounds[1]:
            raise SkillError("invalid_contract")
    elif kind == "string":
        limit = spec.get("maxLength")
        if not isinstance(limit, int) or not _is_int(limit):
            raise SkillError("invalid_contract")
        if not 1 <= limit <= MAX_STRING_LENGTH:
            raise SkillError("invalid_contract")
        if "enum" in spec:
            options = spec["enum"]
            if not isinstance(options, list) or not 1 <= len(options) <= MAX_ENUM:
                raise SkillError("invalid_contract")
            if not all(isinstance(o, str) and len(o) <= limit for o in options):
                raise SkillError("invalid_contract")
            if len(set(options)) != len(options):
                raise SkillError("invalid_contract")


def _check_contract(value: Any) -> None:
    """A closed object of typed scalar fields, the subset planner parameters may use."""
    contract = _closed(
        value,
        frozenset({"type", "additionalProperties", "required", "properties"}),
        "invalid_contract",
    )
    if contract["type"] != "object" or contract["additionalProperties"] is not False:
        raise SkillError("invalid_contract")
    properties = contract["properties"]
    if not isinstance(properties, dict) or len(properties) > MAX_CONTRACT_FIELDS:
        raise SkillError("invalid_contract")
    for name, spec in properties.items():
        if not _FIELD.fullmatch(name):
            raise SkillError("invalid_contract")
        _check_field(spec)
    required = contract["required"]
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise SkillError("invalid_contract")
    if len(set(required)) != len(required) or not set(required) <= set(properties):
        raise SkillError("invalid_contract")


def _value_matches(spec: Mapping[str, Any], value: Any) -> bool:
    kind = spec["type"]
    if kind == "boolean":
        return type(value) is bool
    if kind == "string":
        if not isinstance(value, str) or len(value) > spec["maxLength"]:
            return False
        return "enum" not in spec or value in spec["enum"]
    if not (_is_int(value) if kind == "integer" else _is_number(value)):
        return False
    if "minimum" in spec and value < spec["minimum"]:
        return False
    return "maximum" not in spec or bool(value <= spec["maximum"])


def _check_values(contract: Mapping[str, Any], values: Any) -> None:
    if not isinstance(values, Mapping):
        raise SkillError("invalid_parameters")
    properties = contract["properties"]
    if not set(values) <= set(properties) or not set(contract["required"]) <= set(values):
        raise SkillError("invalid_parameters")
    for name, value in values.items():
        if not _value_matches(properties[name], value):
            raise SkillError("invalid_parameters")


def _check_resource_bounds(value: Any) -> None:
    bounds = _closed(value, frozenset(RESOURCE_CEILINGS), "invalid_contract")
    for name, ceiling in RESOURCE_CEILINGS.items():
        amount = bounds[name]
        floor = 0 if name in _MAY_BE_ZERO else 1
        if not _is_int(amount) or not floor <= amount <= ceiling:
            raise SkillError("invalid_contract")
    if bounds["max_output_bytes"] > bounds["max_total_output_bytes"]:
        raise SkillError("invalid_contract")


def _check_terminal(completion: Any, failure: Any) -> None:
    done = _closed(
        completion, frozenset({"expected_progress", "success_authority"}), "invalid_contract"
    )
    if done["expected_progress"] not in PROGRESS:
        raise SkillError("invalid_contract")
    if done["success_authority"] != "environment_evaluator":
        raise SkillError("invalid_contract")
    fail = _closed(failure, frozenset({"retry_on", "stop_on", "otherwise"}), "invalid_contract")
    if fail["otherwise"] != "return_to_coordinator":
        raise SkillError("invalid_contract")
    lists: list[set[str]] = []
    for key in ("retry_on", "stop_on"):
        items = fail[key]
        if not isinstance(items, list) or len(set(map(str, items))) != len(items):
            raise SkillError("invalid_contract")
        if not all(item in PICK_PLACE_V1_OUTCOMES and item != "completed" for item in items):
            raise SkillError("invalid_contract")
        lists.append(set(items))
    retry_on, stop_on = lists
    if retry_on & stop_on or retry_on & NEVER_RETRY:
        raise SkillError("invalid_contract")


@dataclass(frozen=True)
class Dependency:
    """A dependency on another skill ID within an inclusive version range."""

    skill_id: str
    min_version: int
    max_version: int

    def accepts(self, version: int) -> bool:
        return self.min_version <= version <= self.max_version


def _check_document(document: Any) -> None:
    if not isinstance(document, dict):
        raise SkillError("malformed")
    if len(canonical_json(document)) > MAX_ARTIFACT_BYTES:
        raise SkillError("oversized")
    if document.get("format") != SKILL_FORMAT:
        raise SkillError("unsupported_format")
    digest = document.get("sha256")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise SkillError("malformed")
    if skill_digest(document) != digest:
        raise SkillError("tampered")
    if set(document) != _ARTIFACT_KEYS:
        raise SkillError("malformed")
    skill_id, version = document["skill_id"], document["skill_version"]
    if not isinstance(skill_id, str) or not _ID.fullmatch(skill_id):
        raise SkillError("malformed")
    if not _is_int(version) or not 1 <= version <= _MAX_SAFE_INT:
        raise SkillError("malformed")
    instructions = document["instructions"]
    if not isinstance(instructions, str) or not 1 <= len(instructions) <= MAX_INSTRUCTIONS:
        raise SkillError("malformed")
    applicability = _closed(
        document["applicability"],
        frozenset({"summary", "task_protocols", "required_observations"}),
        "malformed",
    )
    summary = applicability["summary"]
    if not isinstance(summary, str) or not 1 <= len(summary) <= MAX_APPLICABILITY:
        raise SkillError("malformed")
    _unique_strings(applicability["task_protocols"], _ID, 1, 4, "malformed")
    _unique_strings(applicability["required_observations"], _OBSERVATION, 0, 16, "malformed")
    tools = document["tool_permissions"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= 4:
        raise SkillError("malformed")
    for tool in tools:
        entry = _closed(tool, frozenset({"name", "version"}), "malformed")
        if not isinstance(entry["name"], str) or not _TOOL.fullmatch(entry["name"]):
            raise SkillError("malformed")
        if not isinstance(entry["version"], str) or not _TOOL_VERSION.fullmatch(entry["version"]):
            raise SkillError("malformed")
    if len({tool["name"] for tool in tools}) != len(tools):
        raise SkillError("malformed")
    dependencies = document["dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        raise SkillError("malformed")
    for item in dependencies:
        dep = _closed(item, frozenset({"skill_id", "min_version", "max_version"}), "malformed")
        if not isinstance(dep["skill_id"], str) or not _ID.fullmatch(dep["skill_id"]):
            raise SkillError("malformed")
        low, high = dep["min_version"], dep["max_version"]
        if not _is_int(low) or not _is_int(high) or not 1 <= low <= high <= _MAX_SAFE_INT:
            raise SkillError("malformed")
        if dep["skill_id"] == skill_id:
            raise SkillError("dependency_cycle", skill_id)
    if len({dep["skill_id"] for dep in dependencies}) != len(dependencies):
        raise SkillError("malformed")
    _check_contract(document["inputs"])
    _check_contract(document["outputs"])
    _check_resource_bounds(document["resource_bounds"])
    _check_terminal(document["completion"], document["failure"])


@dataclass(frozen=True)
class Skill:
    """One immutable, verified skill artifact.

    Only the canonical bytes are stored; every accessor returns a fresh copy, so callers cannot
    change a registered skill through a returned value. ``verify()`` recomputes the digest.
    """

    skill_id: str
    skill_version: int
    sha256: str
    canonical: bytes

    @property
    def key(self) -> tuple[str, int]:
        return (self.skill_id, self.skill_version)

    @property
    def label(self) -> str:
        return f"{self.skill_id}@{self.skill_version}"

    def document(self) -> dict[str, Any]:
        """A fresh copy of the sealed artifact document."""
        value: dict[str, Any] = json.loads(self.canonical)
        return value

    def verify(self) -> None:
        """Raise ``SkillError("tampered")`` unless the stored bytes still match the identity."""
        try:
            document = self.document()
            _check_document(document)
        except ValueError:  # includes SkillError
            raise SkillError("tampered", self.label) from None
        identity = (document["skill_id"], document["skill_version"], document["sha256"])
        if identity != (self.skill_id, self.skill_version, self.sha256):
            raise SkillError("tampered", self.label)
        if canonical_json(document) != self.canonical:
            raise SkillError("tampered", self.label)

    @property
    def instructions(self) -> str:
        value: str = self.document()["instructions"]
        return value

    @property
    def applicability(self) -> dict[str, Any]:
        value: dict[str, Any] = self.document()["applicability"]
        return value

    @property
    def inputs(self) -> dict[str, Any]:
        value: dict[str, Any] = self.document()["inputs"]
        return value

    @property
    def outputs(self) -> dict[str, Any]:
        value: dict[str, Any] = self.document()["outputs"]
        return value

    @property
    def tool_permissions(self) -> dict[str, str]:
        """Permitted tool name -> required tool version."""
        return {tool["name"]: tool["version"] for tool in self.document()["tool_permissions"]}

    @property
    def resource_bounds(self) -> dict[str, int]:
        value: dict[str, int] = self.document()["resource_bounds"]
        return value

    @property
    def completion(self) -> dict[str, str]:
        value: dict[str, str] = self.document()["completion"]
        return value

    @property
    def failure(self) -> dict[str, Any]:
        value: dict[str, Any] = self.document()["failure"]
        return value

    @property
    def dependencies(self) -> tuple[Dependency, ...]:
        return tuple(
            Dependency(dep["skill_id"], dep["min_version"], dep["max_version"])
            for dep in self.document()["dependencies"]
        )

    @property
    def parameter_schema_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.inputs)).hexdigest()

    def budgets(self) -> Budgets:
        """The resource bounds as ``uzumaki.budgets.Budgets`` for a ``BoundedRun``."""
        bounds = self.resource_bounds
        return Budgets(
            wall_ms=bounds["wall_ms"],
            model_calls=bounds["model_calls"],
            steps=bounds["steps"],
            retries=bounds["retries"],
            max_output_bytes=bounds["max_output_bytes"],
            max_total_output_bytes=bounds["max_total_output_bytes"],
            attempts=bounds["attempts"],
        )

    def check_parameters(self, parameters: Any) -> None:
        """Raise ``SkillError("invalid_parameters")`` unless ``parameters`` meet ``inputs``."""
        _check_values(self.inputs, parameters)

    def check_result(self, result: Any) -> None:
        """Raise ``SkillError("invalid_parameters")`` unless ``result`` meets ``outputs``."""
        _check_values(self.outputs, result)


def load_skill(document: Any) -> Skill:
    """Validate a sealed artifact document and return the immutable ``Skill``.

    Checks, in order: object (``malformed``), canonical size (``oversized``), ``format``
    (``unsupported_format``), digest shape (``malformed``) and value (``tampered``), then the
    fields (``malformed``, ``dependency_cycle`` for a self-dependency) and the contracts, bounds
    and terminal semantics (``invalid_contract``). Tool availability and dependencies are checked
    by ``SkillRegistry.register``.
    """
    _check_document(document)
    canonical = canonical_json(document)
    return Skill(document["skill_id"], document["skill_version"], document["sha256"], canonical)


@dataclass(frozen=True)
class RegistryEvent:
    """One registry change. ``evidence_ref`` is recorded as given and is not verified here."""

    registry_version: int
    action: str  # "registered" or "transition"
    skill_id: str
    skill_version: int
    sha256: str
    from_state: str | None
    to_state: str
    evidence_ref: str | None
    by: str


@dataclass(frozen=True)
class PinnedSkill:
    """A validated skill and its resolved dependency closure, fixed for one run.

    It holds its own immutable artifacts, so registering, deprecating or rejecting anything later
    cannot change what a pinned run executes.
    """

    run_id: str
    registry_version: int
    skill: Skill
    dependencies: tuple[Skill, ...]

    @property
    def skill_id(self) -> str:
        return self.skill.skill_id

    @property
    def skill_version(self) -> int:
        return self.skill.skill_version

    @property
    def sha256(self) -> str:
        return self.skill.sha256

    def verify(self) -> None:
        self.skill.verify()
        for dependency in self.dependencies:
            dependency.verify()


def _tools_within(dependency: Skill, dependent: Skill) -> bool:
    granted = dependent.tool_permissions
    return all(granted.get(name) == v for name, v in dependency.tool_permissions.items())


class RegistrySnapshot:
    """Immutable view of the registry at one ``registry_version``."""

    def __init__(
        self,
        registry_version: int,
        skills: Mapping[tuple[str, int], Skill],
        states: Mapping[tuple[str, int], str],
    ) -> None:
        self.registry_version = registry_version
        self._skills = MappingProxyType(dict(skills))
        self._states = MappingProxyType(dict(states))

    def keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._skills))

    def get(self, skill_id: str, skill_version: int) -> Skill:
        skill = self._skills.get((skill_id, skill_version))
        if skill is None:
            raise SkillError("unknown_skill")
        return skill

    def state(self, skill_id: str, skill_version: int) -> str:
        self.get(skill_id, skill_version)
        return self._states[(skill_id, skill_version)]

    def _validated_versions(self, skill_id: str) -> list[int]:
        return sorted(
            version
            for (sid, version), state in self._states.items()
            if sid == skill_id and state == "validated"
        )

    def _resolve(self, skill: Skill, chain: tuple[str, ...]) -> dict[tuple[str, int], Skill]:
        """Resolve each dependency to its highest validated version in range, recursively."""
        resolved: dict[tuple[str, int], Skill] = {}
        for dep in skill.dependencies:
            if dep.skill_id in chain:
                raise SkillError("dependency_cycle", skill.label)
            if not any(sid == dep.skill_id for sid, _ in self._skills):
                raise SkillError("missing_dependency", skill.label)
            versions = [v for v in self._validated_versions(dep.skill_id) if dep.accepts(v)]
            if not versions:
                raise SkillError("incompatible_dependency", skill.label)
            target = self._skills[(dep.skill_id, versions[-1])]
            if not _tools_within(target, skill):
                raise SkillError("incompatible_dependency", skill.label)
            resolved[target.key] = target
            resolved.update(self._resolve(target, (*chain, dep.skill_id)))
        return resolved

    def resolve_dependencies(self, skill: Skill) -> tuple[Skill, ...]:
        closure = self._resolve(skill, (skill.skill_id,))
        return tuple(closure[key] for key in sorted(closure))

    def pin(self, skill_id: str, skill_version: int | None = None, *, run_id: str) -> PinnedSkill:
        """Pin a validated skill (the highest validated version when none is given)."""
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise SkillError("malformed")
        if skill_version is None:
            versions = self._validated_versions(skill_id)
            if not versions:
                known = any(sid == skill_id for sid, _ in self._skills)
                raise SkillError("not_validated" if known else "unknown_skill")
            skill_version = versions[-1]
        if self.state(skill_id, skill_version) != "validated":
            raise SkillError("not_validated")
        skill = self._skills[(skill_id, skill_version)]
        skill.verify()
        dependencies = self.resolve_dependencies(skill)
        for dependency in dependencies:
            dependency.verify()
        return PinnedSkill(run_id, self.registry_version, skill, dependencies)

    def eligible(self, task_protocol: str | None = None) -> tuple[Skill, ...]:
        """Latest validated version per skill ID whose dependencies resolve, sorted by ID."""
        latest: dict[str, Skill] = {}
        for (skill_id, _), skill in sorted(self._skills.items()):
            if self._states[skill.key] != "validated":
                continue
            if task_protocol is not None:
                if task_protocol not in skill.applicability["task_protocols"]:
                    continue
            try:
                self.resolve_dependencies(skill)
            except SkillError:
                continue
            latest[skill_id] = skill
        return tuple(latest[skill_id] for skill_id in sorted(latest))

    def eligible_skills(
        self,
        *,
        eligible_set_id: str,
        based_on_observation_id: str,
        computed_at_ms: int,
        task_protocol: str | None = None,
    ) -> dict[str, Any]:
        """Contract 1.0 ``eligible_skills`` record (read-only view) for this snapshot."""
        for value in (eligible_set_id, based_on_observation_id):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise SkillError("malformed")
        if not _is_int(computed_at_ms) or not 0 <= computed_at_ms <= _MAX_SAFE_INT:
            raise SkillError("malformed")
        skills = self.eligible(task_protocol)
        if len(skills) > MAX_ELIGIBLE:
            raise SkillError("too_many_eligible")
        return {
            "schema_version": CONTRACT_VERSION,
            "kind": "eligible_skills",
            "payload": {
                "eligible_set_id": eligible_set_id,
                "registry_version": self.registry_version,
                "based_on_observation_id": based_on_observation_id,
                "computed_at_ms": computed_at_ms,
                "access": "read_only",
                "skills": [
                    {
                        "skill_id": skill.skill_id,
                        "skill_version": skill.skill_version,
                        "content_sha256": skill.sha256,
                        "parameter_schema_sha256": skill.parameter_schema_sha256,
                        "tool_permissions": list(skill.tool_permissions),
                        "applicability": skill.applicability["summary"],
                    }
                    for skill in skills
                ],
            },
        }


def _check_tool_catalog(tools: Mapping[str, str]) -> dict[str, str]:
    catalog = dict(tools)
    for name, version in catalog.items():
        if not isinstance(name, str) or not _TOOL.fullmatch(name):
            raise ValueError("tool names must match [a-z][a-z0-9_]{0,63}")
        if not isinstance(version, str) or not _TOOL_VERSION.fullmatch(version):
            raise ValueError("tool versions must look like '1.0'")
    return catalog


class SkillRegistry:
    """Thread-safe registry of immutable skill artifacts and their states.

    ``tools`` is the declared tool catalog (name -> version), for example a broker manifest's
    ``tools``. It defaults to the ``pick-place-v1`` tools at version ``1.0``.
    """

    def __init__(self, tools: Mapping[str, str] | None = None) -> None:
        if tools is None:
            tools = {name: PICK_PLACE_V1_TOOL_VERSION for name in PICK_PLACE_V1_TOOLS}
        self._tools = _check_tool_catalog(tools)
        self._skills: dict[tuple[str, int], Skill] = {}
        self._states: dict[tuple[str, int], str] = {}
        self._digests: dict[tuple[str, int], str] = {}
        self._events: list[RegistryEvent] = []
        self._version = 1
        self._lock = threading.Lock()

    @property
    def registry_version(self) -> int:
        return self._version

    @property
    def tools(self) -> dict[str, str]:
        return dict(self._tools)

    def history(self) -> tuple[RegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> RegistrySnapshot:
        with self._lock:
            return RegistrySnapshot(self._version, self._skills, self._states)

    def get(self, skill_id: str, skill_version: int) -> Skill:
        return self.snapshot().get(skill_id, skill_version)

    def state(self, skill_id: str, skill_version: int) -> str:
        return self.snapshot().state(skill_id, skill_version)

    def pin(self, skill_id: str, skill_version: int | None = None, *, run_id: str) -> PinnedSkill:
        return self.snapshot().pin(skill_id, skill_version, run_id=run_id)

    def verify(self) -> None:
        """Re-verify every stored artifact against its digest at registration."""
        with self._lock:
            entries = list(self._skills.items())
            digests = dict(self._digests)
        for key, skill in entries:
            skill.verify()
            if skill.sha256 != digests[key]:
                raise SkillError("tampered", skill.label)

    def _check_tools(self, skill: Skill) -> None:
        for name, version in skill.tool_permissions.items():
            declared = self._tools.get(name)
            if declared is None:
                raise SkillError("unknown_tool", skill.label)
            if declared != version:
                raise SkillError("tool_version_mismatch", skill.label)

    def _reaches(self, start: str, target: str) -> bool:
        """Whether any registered version of ``start`` depends, transitively, on ``target``."""
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            for (sid, _), skill in self._skills.items():
                if sid == current:
                    pending.extend(dep.skill_id for dep in skill.dependencies)
        return False

    def _check_dependencies(self, skill: Skill) -> None:
        for dep in skill.dependencies:
            registered = [s for (sid, _), s in self._skills.items() if sid == dep.skill_id]
            if not registered:
                raise SkillError("missing_dependency", skill.label)
            usable = [
                s
                for s in registered
                if dep.accepts(s.skill_version) and self._states[s.key] != "rejected"
            ]
            if not usable:
                raise SkillError("incompatible_dependency", skill.label)
            if not all(_tools_within(target, skill) for target in usable):
                raise SkillError("incompatible_dependency", skill.label)
            if self._reaches(dep.skill_id, skill.skill_id):
                raise SkillError("dependency_cycle", skill.label)

    def register(self, artifact: Skill | Mapping[str, Any], *, by: str = "caller") -> Skill:
        """Register a new artifact as ``candidate``. Nothing is ever replaced."""
        skill = artifact if isinstance(artifact, Skill) else load_skill(artifact)
        skill.verify()
        with self._lock:
            existing = self._skills.get(skill.key)
            if existing is not None:
                same = existing.sha256 == skill.sha256
                raise SkillError("duplicate_skill" if same else "conflicting_skill", skill.label)
            versions = [version for sid, version in self._skills if sid == skill.skill_id]
            if versions and max(versions) > skill.skill_version:
                raise SkillError("conflicting_skill", skill.label)
            if len(self._skills) >= MAX_SKILLS:
                raise SkillError("oversized")
            self._check_tools(skill)
            self._check_dependencies(skill)
            self._skills[skill.key] = skill
            self._states[skill.key] = "candidate"
            self._digests[skill.key] = skill.sha256
            self._version += 1
            self._events.append(
                RegistryEvent(
                    self._version,
                    "registered",
                    skill.skill_id,
                    skill.skill_version,
                    skill.sha256,
                    None,
                    "candidate",
                    None,
                    by,
                )
            )
        return skill

    def transition(
        self,
        skill_id: str,
        skill_version: int,
        to_state: str,
        *,
        evidence_ref: str,
        by: str = "caller",
    ) -> int:
        """Change the state of one version; returns the new ``registry_version``.

        ``validated`` additionally requires the artifact to verify and every dependency to resolve
        to a validated version (``incompatible_dependency`` otherwise).
        """
        if not isinstance(evidence_ref, str) or not _ID.fullmatch(evidence_ref):
            raise SkillError("malformed")
        if to_state not in STATES:
            raise SkillError("invalid_transition")
        with self._lock:
            key = (skill_id, skill_version)
            skill = self._skills.get(key)
            if skill is None:
                raise SkillError("unknown_skill")
            current = self._states[key]
            if (current, to_state) not in TRANSITIONS:
                raise SkillError("invalid_transition", skill.label)
            if to_state == "validated":
                skill.verify()
                view = RegistrySnapshot(self._version, self._skills, self._states)
                view.resolve_dependencies(skill)
            self._states[key] = to_state
            self._version += 1
            self._events.append(
                RegistryEvent(
                    self._version,
                    "transition",
                    skill_id,
                    skill_version,
                    skill.sha256,
                    current,
                    to_state,
                    evidence_ref,
                    by,
                )
            )
            return self._version

    def validate(self, skill_id: str, skill_version: int, *, evidence_ref: str) -> int:
        return self.transition(skill_id, skill_version, "validated", evidence_ref=evidence_ref)

    def reject(self, skill_id: str, skill_version: int, *, evidence_ref: str) -> int:
        return self.transition(skill_id, skill_version, "rejected", evidence_ref=evidence_ref)

    def deprecate(self, skill_id: str, skill_version: int, *, evidence_ref: str) -> int:
        return self.transition(skill_id, skill_version, "deprecated", evidence_ref=evidence_ref)


_STATE_PATHS: dict[str, tuple[str, ...]] = {
    "candidate": (),
    "validated": ("validated",),
    "rejected": ("rejected",),
    "deprecated": ("validated", "deprecated"),
}


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise SkillError("malformed")
    return result


def _no_constants(name: str) -> Any:
    raise SkillError("malformed")


def parse_json(data: bytes) -> Any:
    """Strict JSON: UTF-8, no duplicate keys, no NaN or Infinity."""
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constants,
        )
    except SkillError:
        raise
    except (ValueError, RecursionError):
        raise SkillError("malformed") from None


def load_skill_set(path: str | Path, *, tools: Mapping[str, str] | None = None) -> SkillRegistry:
    """Load a hand-authored ``uzumaki.skillset/1.0`` file into a new registry.

    The file is ``{"format", "skills": [{"artifact", "state", "evidence_ref"}]}``. Every artifact
    is registered as ``candidate`` in file order, then moved to its declared state through the
    allowed transitions (``deprecated`` via ``validated``) in file order, so dependencies must be
    listed first. The declared state and ``evidence_ref`` are the author's assertion, recorded
    with ``by="skill_set"``; they are not independent evaluation. Any error rejects the whole set.
    """
    with open(path, "rb") as handle:
        data = handle.read(MAX_SKILL_SET_BYTES + 1)
    if len(data) > MAX_SKILL_SET_BYTES:
        raise SkillError("oversized")
    document = parse_json(data)
    if not isinstance(document, dict):
        raise SkillError("malformed")
    if document.get("format") != SKILL_SET_FORMAT:
        raise SkillError("unsupported_format")
    if set(document) != {"format", "skills"}:
        raise SkillError("malformed")
    entries = document["skills"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SKILLS:
        raise SkillError("malformed")
    registry = SkillRegistry(tools)
    planned: list[tuple[Skill, str, str | None]] = []
    for index, item in enumerate(entries):
        entry = _closed(item, frozenset({"artifact", "state", "evidence_ref"}), "malformed")
        state, evidence = entry["state"], entry["evidence_ref"]
        if state not in _STATE_PATHS:
            raise SkillError("malformed", f"skills[{index}]")
        if state == "candidate" and evidence is not None:
            raise SkillError("malformed", f"skills[{index}]")
        if state != "candidate" and (not isinstance(evidence, str) or not _ID.fullmatch(evidence)):
            raise SkillError("malformed", f"skills[{index}]")
        try:
            skill = registry.register(entry["artifact"], by="skill_set")
        except SkillError as error:
            raise SkillError(error.reason, f"skills[{index}]") from None
        planned.append((skill, state, evidence))
    for index, (skill, state, evidence) in enumerate(planned):
        for step in _STATE_PATHS[state]:
            assert evidence is not None
            try:
                registry.transition(
                    skill.skill_id, skill.skill_version, step, evidence_ref=evidence, by="skill_set"
                )
            except SkillError as error:
                raise SkillError(error.reason, f"skills[{index}]") from None
    return registry
