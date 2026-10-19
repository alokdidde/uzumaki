# Testing and acceptance strategy

This is a test plan, not a report of passing implementation tests. Issue 01 freezes task-specific metrics and thresholds; issue 03 introduces runnable package/CI commands and documents them here. No model API, dependency install, or powered hardware is required to review this bootstrap.

The candidate task profile and thresholds are in the [protocol](protocol/README.md) (`candidate_for_review`, unmeasured). Today's checks are documentary (diff whitespace check, JSON inspection, the external protocol checker, manual review criteria in that README; the profile is review revision 5, a documentary revision with zero trials, `candidate_for_review`, pending PR acceptance); they are not the executable tests described below, which start with feature 03.

Issue 02 adds the [harness and model contracts 1.0](contracts/README.md) with concrete [fixtures](contracts/fixtures.json) for malformed, missing, stale, contradictory, oversized and unsupported-version inputs. They are checked offline by the external trusted checker (`python3 /path/to/alok-factory/scripts/contracts_verify.py /path/to/uzumaki`, which needs the factory `requirements-controller.txt` installed separately). That check validates the schemas and the expected fixture reasons only; it executes no application code. Admission, fencing and lifecycle enforcement remain future contract/unit tests (feature 03 onward), and passing the offline check is not evidence that any runtime enforces the contracts.

## Acceptance protocol

Before implementing a feature, record its workload, pinned inputs and environment, acceptance criteria, failure cases, metrics, immutable thresholds, clean-checkout reproduction commands, and retained evidence location. Document protocol changes and rerun affected checks; never weaken a valid check to turn a failure into a pass. Every feature has specific acceptance and test cases in [the roadmap](roadmap.md) and its issue.

Correctness/containment invariants are mandatory, and their absence must be shown by retained independent evidence: for the first task all 40 held-out cases, failures included, need integrity-checked invariant evidence, missing or corrupt evidence is never read as zero, and failure counts alone never prove zero violations (see `acceptance_evidence_rules` in the [protocol](protocol/README.md#acceptance-evidence-rules-acceptance_evidence_rules)). Rejected or fenced requests are diagnostics; only actual prohibited admission, execution, effects or access count as violations. performance or success-rate targets depend on the chosen task. Do not invent universal latency or robotics success thresholds. Unimplemented or unavailable checks are not passes. An optional suite may be excluded from a default run; when explicitly requested, missing prerequisites produce a visible failure.

## Layers

| Layer | Environment and purpose | Required negative evidence |
| --- | --- | --- |
| Contract/unit | Fake clock, versioned fixtures, recorded providers | Malformed/oversized input, incompatible versions, stale data, invalid lifecycle transitions |
| Offline integration | Goal through rule/model fixture selection, worker execution, independent outcome and events | Wrong success claim, no progress, retry exhaustion, cancellation and late result |
| Worker containment | Real restricted OS worker, no API credentials or network | Host file escape, secret/network access, process escape, hang/crash, memory/output exhaustion |
| Provider contract | Recorded requests/responses and fault injection | Timeout, rate limit, disconnect, invalid schema, missing usage and unknown model version |
| Live provider smoke | Explicit opt-in with fixed inference budget | Failure behavior and redaction; never claim repeatable fresh-model outputs |
| Physics integration | Pinned simulator/models/assets and seeded variations | Failed reset/readiness, unreachable target, misgrasp, collision/contact or placement failure |
| Held-out evaluation | Frozen candidate, memory and protected task split; no repair | Failures, abstentions, interventions, verifier disagreement, distribution changes |
| Regression/release | Locked versions, clean checkout, independent reviewer | Missing evidence, corrupt replay, promoted-artifact mismatch, secret leakage |
| Model research | Versioned permitted dataset and frozen baseline/compute budget | Leakage, label defects, memorization, calibration/OOD failure, task regression |
| Hardware extension | Separately reviewed profile and guarded protocol | Model/network/harness loss, stale state, reset failure, partial effects; no powered work before prerequisites |

## Baselines and ablations

First establish hand-authored skills with a rule-based selector. Compare model planning on that same library, then generation/repair under matched budgets. Later compare memory on/off, serial/parallel search, specialized selector/general model, and adapted generator/unchanged generator. Use paired task instances where possible and retain sample counts and uncertainty. Optimize total task outcomes, not only local model accuracy or syntactic validity.

## Metrics

Record independently verified task success, per-attempt success and success with a stated retry allowance, full-sequence completion, interventions, failure reasons, reset reliability, candidate acceptance, repairs/experiments to acceptance, tool/model latency distributions and deadline misses, calls/tokens/cost, total wall time, and resource use when measured. Include failures/timeouts in denominators. Provider-reported missing cost or usage is unknown, not zero. Tail latency needs enough samples; never label a small sample a portable timing guarantee.

Model experiments additionally assess abstention versus coverage, calibration with outcome-backed labels, and transfer to held-out task/scene/object groups. A confident typed choice can still be wrong. A good prediction score, teacher preference, or visually plausible rollout cannot replace independently observed task success.

## Reproduction and retention

Each accepted result binds Git revision and clean/dirty status, dependency lock, OS/platform, simulator/model/asset hashes, provider/model/prompt IDs, dataset/split/seed IDs, exact command, exit status, raw events, summaries, and artifact digests. Default CI must never use paid inference. Replaying recorded responses does not retest a live model. Resimulation is separate from semantic replay and physical reproduction.

Keep the protected holdout outside candidate worker access, prompts, retrieval, and exported training data. Generated code cannot read evaluator files. Separate development feedback from held-out acceptance. If evaluation cases become development input, retire them from the protected holdout and create a new versioned split before making further generalization claims.

Retain raw evidence in an access-controlled location with a retrieval manifest and hashes; redaction or loss that removes required replay inputs makes the corresponding replay claim unavailable. Logs must not expose credentials or private observations. Do not commit large bundles or model weights to ordinary Git history.

## Completion checklist

- Frozen feature criteria and all required tests pass on the final revision.
- Negative/failure paths have observed outcomes, not just prose expectations.
- Reported metrics recompute from retained evidence and identify scope/limits.
- Reviewer can reproduce the documented command from a clean checkout.
- Issue links evidence and actual commands; Project status changes only after acceptance.

## Offline command (issue 03)

`make check` runs ruff lint and format check, `mypy src`, `pytest` and an offline `python -m build --no-isolation`, using only the pinned `requirements-dev.txt` tools. Tests use the recorded provider and block sockets. See [package bootstrap](features/package-bootstrap.md).

## Evidence and replay tests (issue 04)

Part of the same `make check`. `tests/test_evidence.py` records a deterministic toy run with a fake clock and checks the recorded IDs, versions, observation identities, decisions, tool calls, outcome and timings. It rejects truncated (missing tail, missing middle event, cut file), reordered (swapped events, also with a rebuilt checksum chain; backwards time), corrupt (edited data, manifest identity, checksum or head; extra events; bad bytes), incomplete, malformed, oversized and version-mismatched evidence, plus undeclared redactions and false replayability claims. `tests/test_replay.py` replays recorded decisions with the provider, the fake environment and sockets disabled, compares semantic outcomes, detects divergent harness logic and version mismatches, refuses non-replayable runs, and confirms that fresh-model and environment modes are rejected without running anything. See [execution events and semantic replay](features/event-evidence.md). Replay here exercises harness logic against recorded inputs; it does not retest a model or simulator.

## Lifecycle, budget and failure tests (issue 05)

Part of the same `make check`. Logic tests use a fake clock: `tests/test_lifecycle.py` checks the states and edges against contract 1.0, idempotent and racing terminal requests, monotonic progress and its provenance (harness self-report cannot complete a run). `tests/test_budgets.py` checks budget validation and unknown-versus-zero usage. `tests/test_supervisor.py` injects provider timeouts, late completions, results and voluntary stops at or after deadlines, retry storms, malformed and oversized results, worker and tool failures, budget exhaustion, stale run/attempt/call results, cancellation during calls and threaded cancel/completion races. It asserts finite termination and no provider or tool call after a terminal state. `tests/test_deadline.py` is separate and uses real child processes and the real clock: it covers deadline kills, process-group cleanup, crashes, output floods, and `BoundedRun` timing out a hung real provider. Its timing bounds are generous ceilings, not latency claims. See [lifecycle and budgets](features/lifecycle-budgets.md).

## Restricted-worker containment tests (issue 06)

`tests/test_sandbox_admission.py` and `tests/test_broker.py` run on any host inside `make check`: unverified platforms and exposed protected paths are refused before any process starts, and undeclared, wrong-version or malformed tool requests never reach a host handler. `tests/test_sandbox_containment.py` is the worker-containment layer: real restricted processes and the real clock, no credentials or network. Adversarial fixtures attempt file escape (including writes to evaluator, prompt, skill and held-out fixtures, whose digests must be unchanged), credential access, TCP/UDP/UNIX/netlink and raw-syscall networking, subprocess, exec, signal and namespace escape, busy and sleeping hangs, CPU and memory exhaustion, output and file floods, broker abuse and crashes. Every test asserts that no process of the worker group survives and that the host stays responsive. On a host that fails admission the suite is skipped with the reason; that skip is not a pass. `make containment` runs the three files with `UZUMAKI_REQUIRE_CONTAINMENT=1`, which turns a non-admitted host into a failure. Timing bounds are generous ceilings, not latency claims. See [restricted worker](features/restricted-worker.md) for the boundary, the admitted platform and limitations.

## Skill registry tests (issue 07)

Part of the same `make check` (contract/unit layer, offline, no provider, worker or simulator). `tests/test_registry.py` seals the hand-authored `pick-place-v1` set in `tests/fixtures/skills_pick_place_v1.json` and loads it with model-provider construction disabled. It checks every stored field and the digest binding. It rejects tampered artifacts (in files, in sets and in memory), malformed digests and fields, unsupported formats, oversized input, invalid input/output contracts, bounds and terminal semantics, missing tools and tool version mismatches, missing, out-of-range, rejected, over-permissioned and cyclic dependencies, duplicate and conflicting IDs and versions, and disallowed state transitions, each with a fixed reason. It shows that a pinned execution stays byte-identical after new versions, deprecations and rejections, and that earlier snapshots stay unchanged. It also validates the emitted eligible view against the contract 1.0 `eligible_skills` JSON Schema. Only this suite: `python -m pytest tests/test_registry.py`. See [skill registry](features/skill-registry.md).

## Offline integration: task-execution baseline (issue 08)

Part of the same `make check` (offline integration layer: fake clock, scripted fixture world, no provider, worker or simulator). `tests/test_baseline.py` runs all eight frozen cases of `tests/fixtures/baseline_cases_v1.json` (`nominal`, `unreachable-goal`, `failed-grasp`, `stale-observation`, `stale-after-pick`, `retry-exhausted`, `cancellation`, `worker-crash`). Each run goes from goal through observation, eligibility, the rule-based selector, bounded hand-authored skills and retries to the independent fixture evaluator and a final event bundle. Every frozen correctness check must pass, and each case's recorded-decision replay must match a fresh execution. The world, evaluator and selector are made to fail if replay touches them. Negative evidence covers divergent replay, changed versions, tampered bundles, six kinds of invalid selector decision, an ineligible skill, a tool outside a skill's permissions, and motionless (`hold-still`) and report-only (`report-success`) candidates, which must be rejected while a positive control that moves the box is accepted. Only this suite: `python -m pytest tests/test_baseline.py`. To retain the raw bundles and a SHA-256 manifest in `build/evidence/baseline/` (git-ignored), run `make baseline-evidence`. See [offline baseline](features/offline-baseline.md). Passing it is not evidence of simulator, physical, held-out or live-model behavior.
