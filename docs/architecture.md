# Uzumaki architecture

Status: proposed initial architecture. Issue 01 freezes the first operational and evaluation profile; later decisions may refine mechanisms while preserving the ownership below. No runtime capability is implemented by this document.

## Responsibilities

| Component | Owns | Must not own |
| --- | --- | --- |
| Harness coordinator | Task state, orchestration, role invocation, budgets, cancellation, evidence, promotion workflow | Model-owned acceptance criteria |
| Planner role | Proposals to select an eligible skill, request information, abstain, or request a new capability | Arbitrary code execution or unverified completion |
| Generator role | Candidate artifacts and evidence-guided repairs | Evaluator, held-out assets, promoted skill mutation, unrestricted tool access |
| Context assembler | Relevant observations/history/guidance with provenance and bounded size | Hidden evaluation answers or treating retrieved text as authority |
| Skill registry | Immutable versions, applicability, permissions, dependencies, and evidence links | Silent replacement of running skill versions |
| Restricted worker and tool broker | Bounded execution of declared operations in a tested isolation boundary | Credentials, unrestricted host filesystem/network, evaluator writes |
| Evaluator | Independent task outcomes, mandatory invariants, frozen comparisons, acceptance decisions | Accepting a model's claim of success as ground truth |
| Simulator/environment adapter | Observations, tool effects, reset/readiness, independently observed progress | Unsupported equivalence to physical hardware |

## Two loops and one initial model

The execution loop observes, selects a validated skill, validates its applicability and inputs, executes within budget, and records the outcome. A skill handles ordinary branches and retries; unfamiliar failures, budget exhaustion, or completion return control to the coordinator.

The improvement loop gives the generator a specific missing capability and allowed interfaces. Candidates pass static checks, isolation checks, development trials and repairs, then held-out evaluation. Only frozen candidates reach held-out evaluation, without test-specific repair or memory updates. Promotion produces a new immutable version. Failure evidence from held-out evaluation does not reenter development; a changed evaluation protocol requires a fresh protected holdout and versioned comparison.

One underlying pretrained model initially serves planner and generator roles. Contexts, permissions, output schemas, and budgets remain separate even when model weights are shared. No dedicated decision model or neural training pipeline is required to demonstrate MVP behavior. New skills are initially knowledge and executable artifacts, not updates to model weights.

## Context and contracts

Separate observed facts from estimates and judgments, objectives from hints, and proposals from permitted execution. Each observation has source, capture time, version, and applicable freshness semantics. Skill instructions, tool outputs, and retrieved memory are untrusted data at model boundaries. A selected skill ID and parameters are validated against the current eligible set before execution; earlier validation does not establish current applicability.

A changed goal produces an explicit cancellation or transition request. Fence late results with run and attempt IDs. Do not rewrite completed effects or report that cancellation reversed physical progress. Status includes completed, failed, blocked, abstained, cancelled, timed out, and partial progress; exact transition rules are defined by feature 02 in the [harness contracts 1.0](contracts/README.md) ([v1.json](contracts/v1.json) `lifecycle`: `queued`, `running`, `cancelling`, and terminal `completed`, `cancelled`, `failed`, `rejected`, with no edges leaving terminal states, cancellation that never reverses progress, and late-result fencing). Those contracts also fix the record schemas, the authority map (planner selects or abstains only; generator artifacts are untrusted with `authorization: false`; the environment evaluator alone authors outcomes) and admission as a harness-only decision separate from proposals. They are documentary; no runtime enforces them until feature 03 onward. For the first task, the candidate profile already fixes the primary-outcome precedence and violation counting: requests rejected or fenced before admission are diagnostics, and only actual prohibited admission, execution, effects or access are violations.

## Representation and containment

Choose one candidate representation in feature 12: a constrained program or a typed executable graph. Graph validation catches structural errors but does not establish semantic correctness. Programs and graph nodes still require an OS-enforced restricted worker, a permissioned tool broker, and independent task verification. A worker is not a physical safety controller.

No generated code runs in the harness parent process. Static checks and exception catching are not a sandbox. The host watchdog must remain responsive when a worker loops, crashes, allocates excessively, or floods output. Unsupported isolation environments reject execution.

## Evidence and evaluation

Record inputs, relevant observations, skill/model/prompt versions, candidate lineage, tool results, budgets, outcomes, and artifact identities. Recorded-decision replay is distinct from fresh-model reevaluation and physics resimulation. Exact trace equality supports same-artifact replay or behavior-equivalent adapters; task improvement can legitimately change trajectories while preserving mandatory invariants.

Freeze baselines, budgets, splits, metrics and thresholds before evaluating a claimed gain. Independently measured outcomes determine labels; teacher choices and confidence remain model judgments. Recorded failures and timeouts count in denominators. Defaults are offline and bounded; paid providers and simulator suites are opt-in.

## Open choices

Issue 01 proposes, as a `candidate_for_review` pending PR acceptance ([protocol](protocol/README.md), [decision 0001](decisions/0001-task-and-evaluation.md)), the initial task, simulator, deployment environment, model-provider candidate (MuJoCo 3.14.0 and `claude-sonnet-5-5`, both unverified) and evaluation envelope. The profile (review revision 5, a documentary revision before acceptance, not a performance result) fixes the tabletop frame, deterministic baseline, shared attempt accounting (one increment per attempt-starting observe; third attempt may complete; no fourth attempt or model call 21), mandatory invariants (only actual prohibited admission, execution, effects or access count; rejected or fenced requests are audit diagnostics) and first-terminal-event outcome precedence (15 named outcomes; every rejected pick/place maps to exactly one, with `tool_rejected` as the catch-all for lifecycle, precondition and argument rejections), including `evaluation_failure` for reset, crash and invalid-evidence faults that stay in the denominator, and `acceptance_evidence_rules` (independently verifiable invariant evidence for all 40 cases; missing or corrupt evidence makes acceptance unavailable, never zero violations), all of which the independent evaluator must implement; no such evaluator exists yet. Feature 03 pins the concrete Python toolchain and dependencies; feature 06 pins a tested worker isolation mechanism; feature 12 chooses one candidate representation. None of these choices implies a complete SDK, universal robot interface, physical safety qualification, or foundation-model training commitment.

The [roadmap](roadmap.md) controls sequence; the [testing strategy](testing.md) controls evidence expectations.

## Implemented so far

`src/uzumaki` contains the `uzumaki.providers.RecordedProvider` fixture replayer (stdlib-only, no network) and the feature-04 evidence library ([feature note](features/event-evidence.md)):

- `uzumaki.evidence` records run/task/candidate IDs, contract, skill and model versions, observation identities, decisions, tool calls and results, the outcome and timings in a bounded, checksum-chained log (`uzumaki.events/1.0`). It redacts sensitive keys and marks a run non-replayable when a replay input was redacted. Its validator rejects truncated, reordered, corrupt, incomplete, oversized and version-mismatched evidence.
- `uzumaki.replay` keeps recorded-decision replay, fresh-model reevaluation and environment resimulation distinct. Only recorded-decision replay is implemented; it serves recorded outputs to deterministic harness logic without a provider or environment and compares semantic outcomes. The other modes are rejected, never substituted.

- `uzumaki.lifecycle`, `uzumaki.budgets` and `uzumaki.supervisor` (feature 05, [feature note](features/lifecycle-budgets.md)) enforce the contract 1.0 lifecycle with idempotent terminal transitions. They add harness statuses (completed, abstained, blocked, partial progress, timeout, budget exhausted, cancelled, failed, rejected) and monotonic progress. `BoundedRun` enforces wall time, model calls, steps, retries, attempts, output bytes and known tokens/cost, with an explicit policy for unknown usage. It fences late results by run, attempt and call ID, makes no call after the run leaves `running`, and releases registered resources once. `uzumaki.deadline` bounds a call in a killable child process group; it is not a sandbox.

No full harness loop, sandbox, evaluator or live model support is implemented. The checksums are integrity checks, not signatures.
