# Feature 02: versioned harness and model contracts

Issue: [02 Define versioned harness and model contracts](https://github.com/alokdidde/uzumaki/issues/2). Stage: Foundation, MVP. Depends on [issue 01](https://github.com/alokdidde/uzumaki/issues/1) (`pick-place-v1`, review revision 5, [protocol](../protocol/README.md)).

Status: **documentary, candidate for review.** Nothing here is a runtime capability or a passing test of one.

## What changed

- [docs/contracts/v1.json](../contracts/v1.json) adds contract `1.0`. It defines eight closed, reference-free JSON Schema Draft 2020-12 record schemas (`task_context`, `observation`, `eligible_skills`, `planner_decision`, `candidate_artifact`, `execution_event`, `outcome`, `model_provenance`). It also covers the authority map, input and freshness limits, validation order, admission checks, harness semantic rules and the run lifecycle.
- [docs/contracts/fixtures.json](../contracts/fixtures.json) adds 22 valid and 42 invalid concrete inputs, each invalid one with a single expected reason.
- [docs/contracts/README.md](../contracts/README.md) is the human-readable specification and fixture catalogue.
- [README](../../README.md), [architecture](../architecture.md) and [testing](../testing.md) now link the contracts and state their limits. [CHANGELOG.md](../../CHANGELOG.md) is added.
- Issue-01 files (`docs/protocol/*`, `docs/decisions/0001-*`) are unchanged.

## Three layers

| Layer | What it is | Status |
| --- | --- | --- |
| Normative contract | `v1.json` schemas, the authority map, admission, lifecycle and semantic rules | Specified here; frozen at 1.0 |
| Offline check | Trusted external checker: schema validity, envelope/authority/limit/outcome/lifecycle invariants, fixture reasons, links | Runnable offline; executes no candidate code |
| Runtime implementation | Admission, fencing, lifecycle enforcement, registry, planner, worker, evaluator | Not implemented; feature 03 onward |

## Decisions

- **One envelope, strict version.** `{schema_version: "1.0", kind, payload}` is closed. Any other version is rejected as `unsupported_version`, with no coercion or downgrade.
- **Categories are separate fields.** Observed facts, inferred judgments, objectives, constraints, hints and partial progress are distinct typed fields in `task_context`, so an item cannot be relabelled. Judgments and hints carry `authoritative: false` and `trust: "untrusted_data"`. Objectives and constraints are protocol constants.
- **Unknown is explicit.** A missing observation field is an absent fact, never a null or a guess. Progress `unknown` is not `none`. Unknown usage is null, never 0. A missing or corrupt invariant evidence count is null, never 0.
- **Single clock domain.** Observation times are integer milliseconds in `harness_monotonic`. The record check is `0 <= received - captured <= 200`. Admission re-checks freshness against the time of the actuating call.
- **Proposals versus admission.** Planner decisions and generator artifacts are proposals. Only the harness coordinator admits actions and records the decision as an execution event. Candidate artifacts have `authorization: false`, and v1 defines no execution grant at all.
- **The evaluator owns success.** `outcome.authoritative_source` is const `environment_evaluator`, and model claims are retained but non-authoritative. The valid fixture `valid-outcome-cancelled-model-claim-ignored` shows a "Task complete." claim alongside primary `cancelled`.
- **Run lifecycle versus task outcome.** Lifecycle `completed` means a valid evaluator terminal event was recorded. Task success is `primary == "completed"`. A run rejected before reset still stays in the denominator as `evaluation_failure`, following the protocol rule that no case is dropped.
- **Objective is the full completion rule.** The `task_context` objective copies every `profile.completion` constant, including speed at most 0.01 m/s, a released object and 0 mandatory invariant violations, so the planner and generator never see a weaker success criterion.
- **Before-attempt actuation is special.** A pick/place before any `observe` is rejected as `actuation_before_observe` and ends the case as `missing_observation`, as `attempt_rules.start` requires. The check runs on harness state before record validation, and the outcome mapping is keyed on the attempt state, so a malformed pick before `observe` also ends the case as `missing_observation`, never `tool_rejected`. Rejections within an active attempt that match no earlier rule map to `tool_rejected`.
- **Outcome consistency is schema-enforced.** The `outcome` schema encodes `lifecycle_to_primary`, `rejected` → progress `none`, `completed` → `goal_reached` with complete evidence and 0 violations, and `collision`/`invariant_violation` with complete evidence → at least 1 violation. A success-bearing inconsistency is rejected as `malformed`.
- **Eligible-set version skew.** A decision whose `eligible_set_id` or `registry_version` differs from the current read-only view is rejected at admission (`eligible_set_version_skew`). Earlier validation does not carry over.

## Limitations

- The offline checker checks the six record-level reasons only. Admission checks, `harness_semantic_rules`, sequence monotonicity, fencing and lifecycle enforcement are normative text and data that no code enforces yet.
- Limits (65536 bytes, 200 ms, per-field lengths and counts) are containment choices, not measurements.
- `candidate_artifact.format` leaves the representation choice to feature 12. Execution authority for promoted artifacts is left to features 06, 16 and 17 under a later contract version.
- The fixtures use development-case geometry and synthetic values only. No heldout material was accessed or invented.

## Reproduction

```
python3 /path/to/alok-factory/scripts/contracts_verify.py /path/to/uzumaki
```

This requires the trusted factory checker and its `requirements-controller.txt` to be installed separately. This issue implements no target package, CI or application runtime. See [contracts README](../contracts/README.md#offline-check-reproduction).
