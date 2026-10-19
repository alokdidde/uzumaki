# Changelog

Documentary changes to canonical contracts and protocols. Entries describe specifications, not runtime capability.

## Unreleased

### Added — execution events and semantic replay ([issue 04](https://github.com/alokdidde/uzumaki/issues/4))

- `uzumaki.evidence`: `EventRecorder` writes a versioned (`uzumaki.events/1.0`, contract `1.0`) event log with run/task/candidate IDs, skill and model versions, observation identities (content digests, not raw content), decisions, tool calls and results, the outcome and timings. Events are SHA-256 chained from the manifest identity. `Limits` bound event count, event bytes and bundle bytes; exceeding them is an error, never silent dropping.
- Sensitive-key redaction at record time with declared `redacted_paths`. Redacting a replay input marks the run non-replayable with a reason.
- `validate_bundle`, `load_bundle` and `write_bundle` reject malformed, oversized, version-mismatched, truncated, reordered, corrupt, incomplete and redaction-inconsistent evidence with fixed `EvidenceError` reasons.
- `uzumaki.replay`: distinct `recorded_decision`, `fresh_model_reevaluation` and `environment_resimulation` modes. Only recorded-decision replay is implemented; it uses no provider or environment and compares semantic outcomes. The other modes raise `UnsupportedReplayMode`. `CLAIM_REQUIREMENTS` and `assess_claims` declare the artifacts each claim needs.
- Tests: `tests/test_evidence.py`, `tests/test_replay.py`, the toy harness `tests/toy_harness.py` and the fixture `tests/fixtures/events_provider.json`. Feature note: [docs/features/event-evidence.md](docs/features/event-evidence.md).

Not changed: the provider API, `make check`, CI, the protocol and contracts 1.0. Not added: a harness loop, the contract `execution_event`/`model_provenance` emitters, signatures, live models or simulators.

### Added — package bootstrap and offline CI ([issue 03](https://github.com/alokdidde/uzumaki/issues/3))

- Stdlib-only `src/uzumaki` package with `py.typed`, pinned Python 3.12.12 and a complete pinned `requirements-dev.txt`.
- `RecordedProvider` (recorded/fake, versioned JSON, no network) with safe `ValueError` failures, a deterministic fixture and unit tests.
- `make check` (ruff, format check, mypy, pytest, offline build) and a read-only-permission CI workflow with SHA-pinned actions. See [docs/features/package-bootstrap.md](docs/features/package-bootstrap.md). No runtime execution or live model support.

### Added — harness and model contracts 1.0 ([issue 02](https://github.com/alokdidde/uzumaki/issues/2))

- [docs/contracts/v1.json](docs/contracts/v1.json) adds contract version `1.0`:
  - eight closed, self-contained JSON Schema Draft 2020-12 record schemas;
  - the authority map: the planner may only select or abstain, the generator produces only untrusted artifacts, and the evaluator is the task-success authority;
  - limits of 65536 input bytes and a 200 ms observation age;
  - the validation order, admission checks, harness semantic rules and the run lifecycle (`queued`, `running`, `cancelling`, then terminal `completed`, `cancelled`, `failed`, `rejected`).
- [docs/contracts/fixtures.json](docs/contracts/fixtures.json) adds 22 valid and 42 invalid concrete fixtures covering malformed, missing, stale, contradictory, oversized and unsupported-version inputs, authority violations, unknown-versus-zero usage and evidence, late-result fencing, cancellation, eligible-set version skew, before-attempt actuation and outcome/lifecycle consistency.
- Review repair 1 (findings r0-F1 to r0-F4): the 200 ms boundary fixture now uses one observation ID throughout; admission adds `actuation_before_observe`, which ends the case as `missing_observation`; the objective carries the full `profile.completion` rule; and the outcome schema enforces lifecycle/primary/progress consistency.
- Review repair 2 (findings r1-r0-F2, r1-r1-F5): the `actuation_before_observe` admission check now runs first, on harness state and before record validation, and the outcome mapping keys the before-attempt rule on the attempt state, so a malformed pick before `observe` ends the case as `missing_observation` rather than `tool_rejected`. The outcome schema now requires at least 1 violation for `collision` or `invariant_violation` with complete invariant evidence. New fixtures cover both.
- [docs/contracts/README.md](docs/contracts/README.md) and [docs/features/harness-contracts.md](docs/features/harness-contracts.md) describe the specification, decisions and limitations.
- [README.md](README.md), [docs/architecture.md](docs/architecture.md) and [docs/testing.md](docs/testing.md) now link the contracts.

Not changed: the issue-01 `pick-place-v1` protocol and decision 0001. Not added: any package, validator code, CI, harness, worker, evaluator, simulator or model integration (feature 03 onward).
