# Testing and acceptance strategy

This is a test plan, not a report of passing implementation tests. Issue 01 freezes task-specific metrics and thresholds; issue 03 introduces runnable package/CI commands and documents them here. No model API, dependency install, or powered hardware is required to review this bootstrap.

The candidate task profile and thresholds are in the [protocol](protocol/README.md) (`candidate_for_review`, unmeasured). Today's checks are documentary (diff whitespace check, JSON inspection, the external protocol checker, manual review criteria in that README); they are not the executable tests described below, which start with feature 03.

## Acceptance protocol

Before implementing a feature, record its workload, pinned inputs and environment, acceptance criteria, failure cases, metrics, immutable thresholds, clean-checkout reproduction commands, and retained evidence location. Document protocol changes and rerun affected checks; never weaken a valid check to turn a failure into a pass. Every feature has specific acceptance and test cases in [the roadmap](roadmap.md) and its issue.

Correctness/containment invariants are mandatory; performance or success-rate targets depend on the chosen task. Do not invent universal latency or robotics success thresholds. Unimplemented or unavailable checks are not passes. An optional suite may be excluded from a default run; when explicitly requested, missing prerequisites produce a visible failure.

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
