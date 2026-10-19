# Versioned skill registry (issue 07)

Status: implemented as an offline library (`uzumaki.registry`). No harness loop, selector, planner or promotion workflow uses it yet.

## Frozen plan

- **Workload:** the hand-authored `pick-place-v1` skill set in `tests/fixtures/skills_pick_place_v1.json` (`observe-scene`, `pick-box`, `place-box`, `pick-and-place` depending on the two, and an unvalidated `push-box` candidate). The tests seal it with `seal_skill` and load it with `load_skill_set`. They also build registries directly and mutate sealed copies to cover the failure cases.
- **Inputs and environment:** Python 3.12.12, stdlib-only runtime, the pinned `requirements-dev.txt` (the tests use `jsonschema` 4.23.0 to check the eligible view against `docs/contracts/v1.json`). Sockets are blocked, `RecordedProvider` construction is made to fail during the offline load test, and no worker, simulator or model runs.
- **Correctness guards:** every artifact field listed in the issue is stored and covered by the digest. A digest mismatch is `tampered`. Lifecycle states are registry metadata, kept separate from the artifact. Only `validated` skills are eligible or pinnable. A pin holds its own immutable artifacts and resolved dependencies. The eligible view validates against the frozen contract 1.0 `eligible_skills` schema.
- **Containment guards:** the artifact limit is 65536 canonical bytes (contract `max_input_bytes`), skill-set files are limited to 1 MiB, and the registry holds at most 256 skills, with at most 32 in an eligible view. JSON parsing is strict (UTF-8, no duplicate keys, no NaN or Infinity). Errors are fixed reason codes and never echo content. The registry executes nothing.
- **Metrics/thresholds:** pass/fail only. Every test in `tests/test_registry.py` must pass inside `make check`.
- **Reproduction:** `python -m pip install -r requirements-dev.txt && python -m pip install --no-deps -e .` once, then `make check` offline. Only this feature: `python -m pytest tests/test_registry.py`.
- **Raw evidence destination:** tests write only to pytest temporary directories. Real registry files and run evidence belong in `runs/` or `artifacts/` (both git-ignored) or an access-controlled store referenced by hash, never in Git.

## Implemented

**Artifact (`uzumaki.skill/1.0`).** A closed JSON object with exactly these fields:

| Field | Rule |
| --- | --- |
| `format` | `"uzumaki.skill/1.0"`; anything else is `unsupported_format`. |
| `skill_id`, `skill_version` | contract ID pattern; integer version ≥ 1 (contract `skill_version`). |
| `sha256` | SHA-256 of the canonical JSON (sorted keys, `,`/`:`, ASCII escapes) of the document without `sha256`. A mismatch is `tampered`. |
| `instructions` | 1–8192 code points. Untrusted data at model boundaries. |
| `applicability` | `summary` (1–512 code points, the contract `applicability` string), `task_protocols` (1–4 IDs such as `pick-place-v1`) and `required_observations` (0–16 field names). |
| `inputs`, `outputs` | closed object contracts (`type: object`, `additionalProperties: false`, `required` ⊆ `properties`, at most 16 lowercase fields) whose fields are `number`/`integer` (optional bounds, min ≤ max), `string` (`maxLength` 1–128, optional `enum`) or `boolean`. This is the planner-parameter subset of contract 1.0. `check_parameters` and `check_result` enforce them (`invalid_parameters`). |
| `tool_permissions` | 1–4 unique `{name, version}` pairs. At registration each must be in the registry's tool catalog (`unknown_tool`) at the same version (`tool_version_mismatch`). The default catalog is the `pick-place-v1` broker tools at `1.0`, and any `ToolBroker.manifest()["tools"]` can be passed. |
| `resource_bounds` | `wall_ms`, `model_calls`, `steps`, `retries`, `attempts`, `max_output_bytes`, `max_total_output_bytes`. These are strict integers ≥ 1 (`model_calls` and `retries` may be 0), with `attempts` ≤ 3 and `model_calls` ≤ 20 (protocol budget), other registry ceilings, and per-call ≤ total output. `budgets()` returns `uzumaki.budgets.Budgets`. |
| `completion` | `expected_progress` (`none`, `partial`, `goal_reached`) and `success_authority: "environment_evaluator"`. A skill can never declare its own success. |
| `failure` | `retry_on` and `stop_on` are disjoint lists of `pick-place-v1` outcomes. `completed` never appears, and `retry_on` excludes the case-ending or violation outcomes in `NEVER_RETRY`. `otherwise: "return_to_coordinator"` hands every unlisted failure back to the coordinator (architecture: unfamiliar failures return control). |
| `dependencies` | up to 8 `{skill_id, min_version, max_version}` ranges with unique IDs. A self-dependency is `dependency_cycle`. |

`load_skill` checks, in order: object (`malformed`), size (`oversized`), format, digest shape (`malformed`) and value (`tampered`), fields (`malformed`), then contracts, bounds and terminal semantics (`invalid_contract`). `Skill` is a frozen dataclass that stores only the canonical bytes, and every accessor returns a fresh copy. `Skill.verify()` recomputes the digest and the identity, so in-memory tampering is detected before a pin.

**Registry.** `SkillRegistry(tools=None)` is thread-safe. The registry starts at `registry_version` 1, and every registration or state change increments it and appends a `RegistryEvent` (action, from/to state, digest, `evidence_ref`, `by`).

- `register(artifact)` adds a new version as `candidate` and never replaces anything. The same artifact again is `duplicate_skill`. The same ID and version with other content, or a version not above the ID's highest, is `conflicting_skill`. Each dependency ID must be registered (`missing_dependency`), with a non-rejected version in range whose tool permissions are within the dependent's (`incompatible_dependency`), and must not lead back to the new skill's ID (`dependency_cycle`). The whole closure must also resolve to one non-rejected version per skill ID that every range on that ID accepts, so an unsatisfiable diamond (for example `helper` needing `place-box` [1, 1] while the new skill needs `place-box` [2, 2]) is `incompatible_dependency`.
- `transition(id, version, to_state, evidence_ref=...)` and the `validate`, `reject` and `deprecate` wrappers allow only `candidate→validated`, `candidate→rejected` and `validated→deprecated` (`invalid_transition` otherwise, with no change). `validated` also re-verifies the artifact and requires the dependency closure to resolve to validated versions, one per skill ID, satisfying every range (`incompatible_dependency`).
- `snapshot()` returns an immutable `RegistrySnapshot` of one version. Its `registry_version` is a read-only property, its maps are read-only proxies, and assigning or deleting any attribute raises `AttributeError` (Python-level immutability, not protection against code that bypasses it with `object.__setattr__`). `eligible(task_protocol)` returns the latest validated version per ID whose dependencies resolve. `eligible_skills(...)` returns the contract 1.0 `eligible_skills` record (`access: "read_only"`, the snapshot's `registry_version`, `content_sha256` = artifact digest, `parameter_schema_sha256` = digest of `inputs`), with at most 32 skills (`too_many_eligible`).
- `pin(id, version=None, run_id=...)` (registry or snapshot) returns a `PinnedSkill`: one validated version (the highest when none is given; `not_validated` for candidate, deprecated or rejected) and its dependency closure. The closure holds exactly one validated version per skill ID, and that version satisfies every range on the ID across the closure. A backtracking search walks dependencies in declaration order and tries the highest version first, so earlier edges get the highest consistent version (in an overlapping diamond where `helper` needs `place-box` [1, 1] and the root accepts [1, 2], both share `place-box@1`). Disjoint ranges are `incompatible_dependency`, and so is a search over more than `MAX_RESOLUTION_STEPS` (10000) edges, which fails closed. All are verified at pin time. The pin holds its own `Skill` objects and the snapshot version, so later registrations, deprecations, rejections or new versions cannot change it. A deprecated dependency blocks new pins (`incompatible_dependency`) but not existing ones.
- `verify()` re-verifies every stored artifact against its digest at registration.

**Hand-authored skill sets.** `load_skill_set(path, tools=None)` reads `uzumaki.skillset/1.0` (`{"format", "skills": [{"artifact", "state", "evidence_ref"}]}`). It registers every artifact as a candidate in file order, then applies the declared state through allowed transitions (`deprecated` via `validated`) with `by="skill_set"`. Any error rejects the whole set and names the entry (`skills[i]`). The repository fixture holds unsealed bodies (no `sha256`), because digests are produced by `seal_skill` at authoring time. Loading it unsealed is rejected as `malformed`, which the tests check.

## Tests

`tests/test_registry.py` (inside `make check`, offline, no provider) covers:

- loading the hand-authored set with provider construction disabled; every stored field, digest binding and `Budgets` conversion; copies returned by accessors; frozen skills;
- tampered artifacts (edited instructions, bounds, tools, failure semantics, a swapped digest), a tampered file inside a set, in-memory byte and identity tampering detected by `verify`, `registry.verify` and `pin`; malformed digests, unsupported formats, oversized artifacts and files, and strict JSON;
- 31 invalid contracts (input/output contracts including non-integer `maxLength`, bounds above the protocol budget or missing, a non-evaluator success authority, retrying violations, overlapping or unknown outcomes) and 17 malformed fields, plus a valid extension that must load;
- missing tools (undeclared, absent from the catalog or a partial broker manifest) and tool version mismatches;
- incompatible dependencies (absent, out of range, only rejected, needing more tools, unvalidated at validation time), self-dependencies and cycles; diamond dependencies: an overlapping diamond pins one shared version, a disjoint diamond is rejected at registration, and a diamond that is consistent only through a candidate blocks validation and, after a deprecation, new pins and eligibility, while an existing pin stays unchanged;
- duplicate artifacts, conflicting content for one ID and version, decreasing versions, duplicate set entries and malformed set entries;
- separation of the four states, eligibility of validated skills only, every disallowed transition leaving the registry unchanged, evidence references and history;
- a pinned execution that stays byte-identical (artifact, dependencies, budgets, parameter contract, snapshot version) after a new dependency version, deprecations, a new dependent version and a rejection; old snapshots stay unchanged; new pins pick the new version; snapshot and registry versions and snapshot maps cannot be reassigned, deleted or mutated;
- the eligible view against the contract `eligible_skills` JSON Schema, protocol filtering, latest-version selection, the 32-skill bound and identifier checks;
- parameter and result checks against the input and output contracts.

## Limitations

- Digests are integrity checks, not signatures. Anyone able to rewrite an artifact can reseal it. Detecting that needs an external anchor such as a signed manifest or retained digests, which is out of scope here.
- State changes record a caller-supplied `evidence_ref` and `by` without verifying them. Hand-authored sets assert their own states. Promotion with structural, containment, behavioural and held-out evidence, blocking self-approved model promotions, and rollback are feature 17. The registry is an in-process library; the contract authority map (planners cannot mutate skills) is enforced by not handing planners a registry object, not by authentication.
- The registry is in memory. `load_skill_set` is a one-shot loader, and there is no persisted registry, export format or event-log integration (`uzumaki.events/1.0` is unchanged).
- Applicability is declared metadata. The registry filters by task protocol only and does not evaluate `required_observations` or the summary against live observations; eligibility against current state, freshness and budgets is harness and planner work (features 08 and 10). Skill instructions are stored, not interpreted. The executable skill representation is chosen in feature 12.
- The failure outcome vocabulary is `pick-place-v1` only. Resource ceilings other than the protocol attempt and model-call budgets are registry ceilings, not measurements.
- Results of `make check` come from CI or a separate validation run; they are not asserted here.
