# Changelog

Documentary changes to canonical contracts and protocols. Entries describe specifications, not runtime capability.

## Unreleased

### Added — lifecycle, budgets, cancellation and failure handling ([issue 05](https://github.com/alokdidde/uzumaki/issues/5))

- `uzumaki.lifecycle`: a thread-safe contract 1.0 run lifecycle (same states, edges and event names). The first terminal request wins. Repeats record `terminal_repeat_ignored` and other disallowed requests record `lifecycle_violation`; neither raises nor changes state. It adds harness statuses `completed`, `abstained`, `blocked`, `partial_progress`, `timeout`, `budget_exhausted`, `cancelled`, `failed` and `rejected`, and monotonic progress (`none`, `partial`, `goal_reached`) that cancellation never reduces. Post-terminal observations are kept separately.
- `uzumaki.budgets`: strict `Budgets` for wall time, model calls (retries included), steps, retries, attempts, per-call and total output bytes, per-call timeouts, and optional tokens and cost. Usage counts only when the provider reports it. Unknown usage is never zero; `unknown_usage` is `fail_closed` (default) or `record_unknown`.
- `uzumaki.supervisor.BoundedRun`: enforces the budgets before and after each model or tool call, and retries timeouts, transient errors and malformed results within budget. Worker failures fail the run. It fences late results by run, attempt and call ID (recorded by digest, never applied) and handles cancellation at call boundaries. It makes no call after the run leaves `running`, releases registered resources exactly once, and provides `execute(logic)` and a `RunReport`.
- `uzumaki.deadline`: `run_in_process` and `ProcessProvider` run a call in a spawned child process group, kill it at the deadline or after the reply, and bound its output. This bounds calls and cleans up processes; it is not a sandbox.
- `uzumaki.faults`: `TransientProviderError`, `ProviderTimeout`, `WorkerFailure`.
- Tests: `tests/test_lifecycle.py`, `tests/test_budgets.py` and `tests/test_supervisor.py` (fake clock), and `tests/test_deadline.py` (real processes), with helpers `tests/fakes.py` and `tests/process_workers.py`. Feature note: [docs/features/lifecycle-budgets.md](docs/features/lifecycle-budgets.md).

- Review repair 1 (findings r0-F1, r0-F2): a voluntary stop at or after the wall deadline now ends the run as `timeout` (`wall_time`), and results arriving exactly at a call deadline are fenced. History entries record the contract `edge` separately from the actual trigger `on`, plus `by` and `evaluated: false`, so harness stops are never labelled as evaluator events. Progress keeps its source (the default is `harness_logic`), and `completed` needs `goal_reached` observed by `environment` or `evaluator`.

Not changed: the `RecordedProvider` API (still usable as a `BoundedRun` provider), the evidence log format `uzumaki.events/1.0`, `make check`, CI, the protocol and contracts 1.0. Not added: a sandboxed worker (feature 06), an evaluator, environment abort/safe-stop, retry backoff, persisted run reports, live models or simulators.

### Added — execution events and semantic replay ([issue 04](https://github.com/alokdidde/uzumaki/issues/4))

- `uzumaki.evidence`: `EventRecorder` writes a versioned (`uzumaki.events/1.0`, contract `1.0`) event log with run/task/candidate IDs, skill and model versions, observation identities (content digests, not raw content), decisions, tool calls and results, the outcome and timings. Events are SHA-256 chained from the manifest identity. `Limits` bound event count, event bytes and bundle bytes. The validator defaults are the ceiling, so the recorder rejects larger limits at construction. Bytes are counted exactly, and room for `run_finished` stays reserved, so exceeding a limit is an error when the offending event is recorded, never in `finish`, and is never silent dropping.
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
