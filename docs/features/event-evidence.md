# Execution events and semantic replay (issue 04)

Status: implemented as an offline library. No harness loop, live model, environment adapter or simulator uses it yet.

## Frozen plan

- **Workload:** a deterministic toy run in `tests/toy_harness.py` (observe, one planner decision from the issue-03 `RecordedProvider` fixture `tests/fixtures/events_provider.json`, pick, place, outcome), and tampered copies of its evidence.
- **Inputs and environment:** Python 3.12.12, stdlib-only runtime, the pinned `requirements-dev.txt`; sockets blocked in tests; fake monotonic clock.
- **Correctness guards:** every listed failure is rejected with a fixed reason; replay never calls a provider or environment; redaction never leaves a run marked replayable when a replay input was removed.
- **Containment guards:** byte and count limits on every event and bundle; errors never echo content; the loader reads at most the byte limit plus one byte.
- **Metrics/thresholds:** pass/fail only. All tests in `tests/test_evidence.py` and `tests/test_replay.py` must pass inside `make check`. No timing or performance claim is made.
- **Reproduction:** `python -m pip install -r requirements-dev.txt && python -m pip install --no-deps -e .` once, then `make check` offline.
- **Raw evidence destination:** tests write only to pytest temporary directories. Real run bundles belong in `runs/` or `artifacts/` (both git-ignored) or an access-controlled store referenced by hash; never in Git.

## Implemented

`uzumaki.evidence` (log format `uzumaki.events/1.0`, bound to contract version `1.0`):

- `EventRecorder(run_id, task_id, candidate_id, skill_versions, model_versions, limits, clock, sensitive_keys)` records `run_started`, `observation`, `decision`, `tool_call`, `tool_result`, `outcome` and `run_finished` events. Each event has a contiguous `seq`, a `monotonic_ms` time from an injectable clock, and `duration_ms` for decisions and tool results.
- Observations keep the ID, source, capture time and a content SHA-256. Raw observation content is not stored. Decisions keep the decision ID, role, the model version declared for that role, the input SHA-256 and the output. Tool calls must name a declared skill version when they name a skill.
- `decide(source, ...)` and `call_tool(tools, ...)` time and record a decision or tool call around any `DecisionSource` or `ToolExecutor`.
- Checksums: event `i` stores `sha256(prev_sha256 + canonical_json(event))`. The chain starts from a digest of the log version and the manifest identity (IDs, contract version, skill/model versions, limits, origin). The manifest stores `event_count` and `head_sha256`.
- Bounded storage: `Limits(max_events=4096, max_event_bytes=65536, max_bundle_bytes=4 MiB)`. These defaults are also the ceiling that `validate_bundle`, `load_bundle`, `write_bundle` and `replay` apply, so the recorder rejects larger limits at construction (`oversized`) and non-positive ones (`malformed`). The recorder counts the exact canonical bytes of the bundle that `finish` will produce, including the manifest totals and `non_replayable` reasons. Until the log is closed, it keeps one event slot and the bytes of the largest possible `run_finished` event in reserve. A recording that would exceed a limit fails when the offending event is recorded, never in `finish`: the recorder raises `oversized` and enters a failed state, so it cannot produce a partial bundle. Once an outcome is recorded, `finish` with a valid status cannot fail on a limit. Nothing is silently dropped.
- Redaction: keys named `api_key`, `apikey`, `authorization`, `credential(s)`, `password`, `secret` or `token` (case-insensitive, at any depth) are replaced by `{"$redacted": "sensitive_key"}` at record time. Each event lists its `redacted_paths`. The fields replay needs are `decision.output`, `tool_call.arguments`, `tool_result.result` and the outcome. A redaction inside one of them sets `manifest.replayable` to `false` with a `required_input_redacted:seq=N:path` reason. A redaction elsewhere (for example `tool_result.diagnostics`) keeps the run replayable.
- `validate_bundle`, `load_bundle` and `write_bundle` (canonical ASCII JSON; no overwrite). Rejections raise `EvidenceError` (a `ValueError`) with a `reason` of `malformed`, `oversized`, `version_mismatch`, `truncated`, `reordered`, `corrupt`, `incomplete`, `redaction_inconsistent` or `recorder_state`. Validation covers structure, log and contract versions, limits, sequence order, missing events or tail, the checksum chain and head, time order, call/result pairing, exactly one outcome right before `run_finished`, decision models against the manifest, and undeclared redactions or a false replayability claim.

`uzumaki.replay`:

- `ReplayMode`: `recorded_decision`, `fresh_model_reevaluation` and `environment_resimulation`. Only `recorded_decision` is supported. The other two always raise `UnsupportedReplayMode` before any runner code runs. No mode falls back to another.
- `replay(bundle, mode, runner, skill_versions=..., model_versions=...)` validates the evidence, refuses non-replayable runs (`NotReplayable`) and version mismatches, and runs the deterministic harness logic `runner` with `RecordedDecisions` and `RecordedTools`. These serve recorded outputs only for identical requests, in order; anything else raises `ReplayDivergence`. No provider or environment is used. The replayed log has `origin: {"mode": "recorded_decision", "source_head_sha256": ...}` so it cannot pose as an original run. `ReplayReport` compares the timing-independent `SemanticOutcome` (observations, decisions, tool calls and results, outcome and status) and lists any differences, including unconsumed recorded decisions or tool calls.
- `CLAIM_REQUIREMENTS` declares the artifacts each claim needs. `assess_claims(bundle)` reports per mode whether the claim is available and what is missing.

## Limitations

- Checksums detect accidental corruption, truncation and reordering. They are not signatures: someone who can rewrite the file can rebuild a consistent chain. Semantic checks still apply to such a chain, but authenticity is not established.
- Redaction is by key name only. Secrets in values under other keys are not detected. Callers must not pass private data under non-sensitive keys.
- The event log is a separate versioned format. It does not yet emit the contract 1.0 `execution_event` or `model_provenance` records; lifecycle, budgets and admission (features 05 onward) are not enforced here.
- Recorded-decision replay checks that harness logic reproduces the same semantics from the same recorded inputs. It does not retest a model or an environment. Fresh-model reevaluation and environment resimulation are not implemented.
- A replayed log declares the same limits as its source, but its `origin` makes the manifest 97 bytes larger. Replaying a recording within that margin of `max_bundle_bytes` therefore fails with `oversized` during replay.
- The toy harness exists only in tests. No real harness, worker, evaluator or simulator records events yet.
- Results of `make check` come from CI or a separate validation run; they are not asserted here.
