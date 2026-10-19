# Harness and model contracts, version 1.0

Status: **documentary contract for [issue 02](https://github.com/alokdidde/uzumaki/issues/2)**, candidate for review. These files are normative specifications and offline-checkable data. No harness, validator package, worker, evaluator, simulator or model integration implements them yet. Executable enforcement starts with feature 03 onward (see [roadmap](../roadmap.md)).

Canonical files:

- [v1.json](v1.json): the contract. It holds `contract_version` `"1.0"`, the `authority` map, `limits`, one self-contained JSON Schema Draft 2020-12 schema per record (no `$ref`), `validation_order`, `harness_semantic_rules`, `admission`, `lifecycle`, `versioning` and `non_claims`.
- [fixtures.json](fixtures.json): concrete valid and invalid inputs, each with one expected rejection reason. These are inputs and expectations, not reports of passing tests.
- Feature summary and decisions: [docs/features/harness-contracts.md](../features/harness-contracts.md). History: [CHANGELOG.md](../../CHANGELOG.md).

The contracts depend on the frozen `pick-place-v1` protocol ([protocol README](../protocol/README.md), [task-v1.json](../protocol/task-v1.json), review revision 5), which they reference and do not change. `outcome.primary` uses exactly the protocol's 15 `profile.outcomes`. Budgets, tools and freshness are copied as constants from `profile.budgets`, `profile.tools` and `profile.observations`.

## Envelope and versioning

Every record is a closed JSON object (`additionalProperties: false`) with exactly the following fields:

| Field | Rule |
| --- | --- |
| `schema_version` | const `"1.0"` (string). An absent value is `missing`. Any other value, including `"1.1"`, `"2.0"` or the number `1.0`, is `unsupported_version`. There is no coercion, no downgrade and no best-effort parse. |
| `kind` | const record name, e.g. `"observation"`. |
| `payload` | closed object with the record's required fields. |

Contract 1.0 is frozen. Any change to a field, enum, limit or rule needs a new `contract_version` and a [CHANGELOG](../../CHANGELOG.md) entry.

## Authority

| Role | `authority` value | Meaning |
| --- | --- | --- |
| planner | `selection_or_abstention_only` | Proposes `select` of one eligible skill or `abstain`. It has no field or record that can register, replace or mutate skills, the registry, evaluator policy, budgets or thresholds. |
| generator | `untrusted_artifact_only` | Emits `candidate_artifact` records with `trust: "untrusted"` and `authorization: false` (const). Contract 1.0 defines no execution grant, so a candidate is never executable under v1. |
| evaluator | `task_success_authority` | Only the environment evaluator authors `outcome` (`authoritative_source: "environment_evaluator"`, const). Model claims are kept in `model_claims[]` with `authoritative: false` and never decide success. |

**Admission is separate from proposals.** `planner_decision`, `candidate_artifact` and model output are proposals. Only the harness coordinator admits an action, and every admission decision is recorded as an `execution_event` (`admission_accepted` / `admission_rejected`). A valid record is necessary but never sufficient for admission. The admission checks, in order, are listed in `v1.json` `admission.checks_in_order`:

1. A `pick`/`place` named by the tool-call channel while the run is `running` and in `between_attempts`, before any `observe` has started an attempt, is `actuation_before_observe`. This check uses harness state only and runs before record validation, so a malformed, contradictory, oversized or unsupported-version pick before `observe` gets the same reason and the same case-ending outcome.
2. Record validation (below).
3. The run is `running`. Otherwise `lifecycle_violation`.
4. Other attempt preconditions hold: an attempt is active, no pick while held, no place while not held, no actuation during settling. Otherwise `lifecycle_violation`.
5. The decision's observation is the latest one, and admission time minus `captured_at_ms` is 200 ms or less. Otherwise `stale`.
6. `eligible_set_id` and `registry_version` match the current read-only view. Otherwise `eligible_set_version_skew`.
7. The `(skill_id, skill_version)` pair is in that view. Otherwise `not_in_eligible_set`.
8. The tool is declared and permitted. Otherwise `unknown_tool`.
9. The budget allows the action: no fourth attempt and no model call 21. Otherwise `budget_exhausted`.
10. Any attempt to mutate the registry or policy, or to execute a candidate, is `unauthorized_operation`.

A rejected pick/place maps to exactly one protocol outcome through `admission.protocol_outcome_mapping.rules_in_order`, which follows `outcome_rules.request_rejections` and `attempt_rules.start`. The first matching rule wins:

| Rejection | Primary | Effect |
| --- | --- | --- |
| Any pick/place rejected in `between_attempts` while `running`, whatever the record's validity (recorded as `actuation_before_observe`) | `missing_observation` | Ends the **case**; no attempt is active, so none is ended, started or counted. If such an actuation were improperly admitted, the primary is `invariant_violation`. |
| `stale` / `missing` observation in an active attempt | `missing_observation` | Ends the active attempt; retry follows `attempt_rules.retry`. |
| Absent or conflicting target | `ambiguous_goal` | Ends the active attempt and may end the case early. |
| Unreachable declared pose | `unreachable_target` | Ends the active attempt and may end the case early. |
| Any other reason within an active attempt (`lifecycle_violation`, `not_in_eligible_set`, `eligible_set_version_skew`, `unknown_tool`, record reasons, `unauthorized_operation`) | `tool_rejected` | Ends the active attempt. |
| `budget_exhausted` (fourth attempt or model call 21) | `budget_exhausted` | Ends the case without invocation. |

Rejected or fenced requests add 0 invariant violations. Rejected non-actuating requests stay diagnostics and do not end an attempt by themselves.

## Records

All times are nonnegative integer milliseconds. All IDs match `^[a-z0-9][a-z0-9._:-]{0,63}$`. Digests are lowercase hex SHA-256. Poses use the tabletop frame (`goal_frame`): `x_m` and `y_m` in [-0.25, 0.25] m, `z_m` in m, `yaw_deg` in [-180, 180]. Free text has explicit `maxLength` limits, and arrays have `maxItems`.

| Record | Required payload fields (plus extras in v1.json) | Key rules |
| --- | --- | --- |
| `task_context` | `task_id`, `observed_facts`, `inferred_judgments`, `objectives`, `constraints`, `hints`, `partial_progress` (+ `context_id`, `case_id`, `assembled_at_ms`) | The six categories are separate typed fields. Facts carry `observation_id` provenance. Judgments carry `author_role` and `authoritative: false`, and `confidence` is null when not stated. Objectives and constraints are const values from the protocol with `source: "task_protocol"`. The objective copies every `profile.completion` condition: `xy_tolerance_m` 0.02, `yaw_tolerance_deg` 10, `settle_s` 0.5, `speed_m_per_s_at_most` 0.01, `require_released_object` true and `mandatory_invariant_violations` 0. Hints have `trust: "untrusted_data"`. Progress is `{level, source, evidence_ref}`, where `unknown` means not assessed and never `none`. |
| `observation` | `task_id`, `case_id`, `run_id`, `attempt_id`, `captured_at_ms`, `received_at_ms`, `observed_facts` (+ `observation_id`, `clock_domain`, `source`) | Both times are in the single `clock_domain: "harness_monotonic"`. If `received < captured` the record is `contradictory`. If `received - captured > 200` it is `stale`. Exactly 200 ms is fresh. A missing field is an absent fact (unknown) and is never null, guessed or filled from memory. |
| `eligible_skills` | `registry_version`, `skills` (+ `eligible_set_id`, `based_on_observation_id`, `computed_at_ms`, `access`) | `access: "read_only"` (const). The view is versioned by `registry_version` and is valid only for its observation. Each skill has an immutable `skill_version`, content and parameter-schema digests, and tool permissions. |
| `planner_decision` | `task_id`, `decision`, `skill_id`, `rationale`, `based_on_observation_id` (+ `decision_id`, `skill_version`, `eligible_set_id`, `registry_version`, `parameters`, `abstain_reason`, `provenance_id`) | `decision` is `select` or `abstain`. `select` needs a non-null `skill_id` and `abstain` needs a null one; otherwise the record is `contradictory`. `rationale` is an inferred judgment of at most 2048 code points. |
| `candidate_artifact` | `artifact_id`, `sha256`, `format`, `authorization` (+ `content`, `parent_artifact_ids`, `repair_round`, `provenance_id`, `trust`) | `authorization: false` (const) and `trust: "untrusted"` (const). `format` is `constrained_program_text` or `executable_graph_json`; feature 12 chooses between them. `content` is at most 65536 code points, and the whole record is still bounded by 65536 bytes. |
| `execution_event` | `case_id`, `run_id`, `attempt_id`, `event_id`, `event_kind`, `monotonic_ms`, `progress` (+ `seq`, `lifecycle_from`, `lifecycle_to`, `tool`, `subject_id`, `rejection_reason`) | Authored by the harness only and append-only. `attempt_id` is null outside an attempt. The closed `event_kind` and `rejection_reason` enums cover admission, cancellation, late-result fencing and terminal-repeat diagnostics. |
| `outcome` | `case_id`, `run_id`, `primary`, `progress`, `evaluator_evidence_refs`, `authoritative_source` (+ `lifecycle_state`, `invariant_evidence`, `invariant_violation_count`, `model_claims`) | `primary` is exactly the 15 protocol outcomes. `progress` is `none`, `partial` or `goal_reached` for every primary. When invariant evidence is `missing` or `corrupt`, `invariant_violation_count` must be null and is never 0. `primary` must be allowed for `lifecycle_state` (`lifecycle_to_primary`); `rejected` requires progress `none`; primary `completed` requires progress `goal_reached`, complete invariant evidence and 0 violations; `abstained` requires progress `none`; `partial_progress` requires progress other than `none`. These are schema `allOf` constraints, so a violation is `malformed`. |
| `model_provenance` | `provider`, `model`, `role`, `prompt_sha256`, `input_sha256`, `output_sha256`, `usage` (+ `provenance_id`, `reported_model`, `replay_mode`, `requested_at_ms`, `latency_ms`) | `usage.status` is `reported`, `partially_reported` or `unknown`. If unknown, all usage values must be null; a guessed 0 is rejected. `output_sha256` and `reported_model` are null when absent. |

## Record validation order (checked offline)

The order is given in `v1.json` `validation_order`. Each input gets exactly one reason.

1. The canonical JSON of the whole input (sorted keys, `,`/`:` separators, ASCII escapes) is larger than 65536 bytes: `oversized`.
2. The input is not an object: `malformed`.
3. `schema_version` is absent: `missing`.
4. `schema_version` is not `"1.0"`: `unsupported_version`.
5. There are schema errors and any of them is `required`: `missing`. Otherwise any schema error (type, const, enum, pattern, bounds or unknown field): `malformed`.
6. In an observation, `received_at_ms < captured_at_ms`: `contradictory`. If the age is over 200 ms: `stale`.
7. In a planner decision, `decision == "select"` does not match `skill_id != null`: `contradictory`.

`harness_semantic_rules` in v1.json lists further normative rules, such as duplicate facts, a digest mismatch, a non-increasing `seq`, and select/abstain coupling of `skill_version` and `abstain_reason`. The offline checker does **not** check these; future runtime features enforce them. Every valid fixture is written to satisfy them as well, so no valid example teaches a rule violation.

## Lifecycle

This covers one harness run per case. The protocol's attempt states (`between_attempts`, `active`, `settling`) live inside `running`, as defined by `profile.attempt_rules`.

| From | To | On |
| --- | --- | --- |
| queued | running | admitted and reset ready |
| queued | rejected | run admission rejected (no effects; case stays in the denominator as `evaluation_failure`, progress `none`) |
| queued | cancelled | cancel before start (progress `none`) |
| queued | failed | reset or readiness failure (`evaluation_failure`) |
| running | completed | evaluator terminal event |
| running | cancelling | cancel requested (`abort` or goal withdrawal) |
| running | failed | component crash or invalid evidence |
| cancelling | cancelled | safe stop reported and evaluated |
| cancelling | failed | component crash or invalid evidence |

The terminal states are `completed`, `cancelled`, `failed` and `rejected`. No transition leaves them, and a terminal outcome is never rewritten. Repeating the identical terminal transition is a no-op recorded as `terminal_repeat_ignored`. Any other request is `lifecycle_violation`. Lifecycle `completed` means "a valid evaluator terminal event was recorded", **not** task success. Success is `outcome.primary == "completed"`. `lifecycle.lifecycle_to_primary` gives the allowed primaries for each terminal state, and the `outcome` schema enforces it (a mismatch is `malformed`). The schema also requires at least 1 violation when the primary is `collision` or `invariant_violation` and invariant evidence is complete.

- **Cancellation effects.** No new actuation is admitted once a run is `cancelling`. `abort` requests a safe stop and reports the actual reached state. Physical progress is never reported as reversed: a cancelled run after a lift records progress `partial`.
- **Late-result fencing.** Results carry `case_id`, `run_id` and `attempt_id`. A result that does not match the active attempt, or that arrives during `cancelling` or a terminal state, is recorded as `late_result_fenced`. It is never applied, cannot authorize a command or revise an outcome, and adds 0 violations. Actual prohibited effects are kept as separate invariant evidence (`outcome_rules.post_terminal`).
- **Racing terminals.** The first terminal case event wins. Simultaneous events use `profile.outcome_rules.priority`.

## Fixture catalogue

[fixtures.json](fixtures.json) uses only development-case geometry (`dev-001`, `dev-002`) and synthetic values. No heldout seed, geometry or result was read, used or invented.

- **Valid (22):** at least one per record. They cover:
  - a fresh observation, the 200 ms boundary, and an observation with no facts (all unknown);
  - select and abstain decisions;
  - a fenced late result, an admission rejected for eligible-set version skew, a pick rejected before any observe (`actuation_before_observe`), a malformed pick before any observe rejected with the same reason (subject null) together with its case outcome `missing_observation` (progress `none`, 0 violations), and a candidate whose execution is refused (`unauthorized_operation`);
  - a running→cancelling event with partial progress;
  - a cancelled outcome whose model claim "Task complete." is ignored, a completed outcome with `goal_reached` and 0 violations, a `collision` outcome with 1 violation, and an outcome with missing invariant evidence (count null);
  - provenance with unknown usage and with reported usage.
- **Invalid (42):** each input is otherwise valid, so exactly one reason applies.
  - `malformed`: non-object input; wrong types and bounds; wrong clock domain; a null fact; wrong `kind`; authority violations (a planner `register_skill` field, a writable eligible view, `authorization: true`, a trusted candidate, an outcome sourced from the planner, an authoritative judgment, an objective from retrieval, progress without the evaluator); an unknown primary; missing evidence read as 0; unknown usage guessed as 0; a relaxed objective speed; outcomes whose primary or progress contradicts the lifecycle state or completion rule (a rejected or failed run claiming `completed`, `completed` with a violation, with missing evidence or with progress `partial`, `cancelled` in a `completed` run, and `collision` or `invariant_violation` with complete evidence but 0 violations).
  - `missing`: no `schema_version`, `received_at_ms`, `payload`, `skill_id` key, `usage`, or objective `require_released_object`.
  - `stale`: 250 ms and 201 ms.
  - `contradictory`: reversed times; select with a null skill; abstain with a skill.
  - `oversized`: a literal 65,536-character `content` string, which makes the record 65,858 canonical bytes.
  - `unsupported_version`: `"2.0"`, `"1.1"` and numeric `1.0`.

## Offline check (reproduction)

From a clean checkout, with the trusted factory checker and its `requirements-controller.txt` installed separately (outside this repository; this issue adds no package, lock or CI):

```
python3 /path/to/alok-factory/scripts/contracts_verify.py /path/to/uzumaki
```

The checker validates every record schema against Draft 2020-12 and rejects any `$ref`. It confirms the closed envelopes, required fields, authority and limits, the outcome enum against `task-v1.json`, the lifecycle states, edges and terminal states, and that every fixture's actual reason equals the expected one. It also checks relative documentation links. It executes no candidate or application code. `git diff --check` and confirming that `docs/protocol/` and `docs/decisions/` are unchanged complete the documentary checks.

## Limits of this contract

- These contracts are documentary. A passing offline check shows that the schemas and fixtures are mutually consistent, not that any runtime enforces them.
- Admission, semantic rules, fencing and lifecycle enforcement are future runtime work: events and replay (04), lifecycle and budgets (05), worker (06), registry (07), planner (10).
- The 65536-byte and 200 ms limits come from the issue-02 scope and the protocol. They are containment limits, not measured performance.
- Model IDs in fixtures are labels only. No model was called.
