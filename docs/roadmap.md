# Implementation roadmap

Status: planning only. All 26 features are open; no implementation or experiment is marked complete.

Implement required work in ascending Sequence order. Dependencies must be accepted before a feature becomes Ready. Feature 01 is initially Ready; later required items are Blocked. Optional experiments are Deferred and each lists its own prerequisites; they do not form a mandatory chain.

The MVP is features 01–18. Features 19–21 establish transfer, longer tasks, and an outcome-backed dataset. Features 22–26 are optional research or deployment extensions. This order is a starting plan, not a claim that all features are necessary for the first release.

Track execution in the [private GitHub Project](https://github.com/users/alokdidde/projects/4). Read [architecture](architecture.md), [testing](testing.md), and [contributing](../CONTRIBUTING.md) first.

## Ordered overview

| Sequence | Feature | Stage | Scope | Prerequisites |
| --- | --- | --- | --- | --- |
| 01 | [Freeze the first task and evaluation protocol](https://github.com/alokdidde/uzumaki/issues/1) | Foundation | MVP | None |
| 02 | [Define versioned harness and model contracts](https://github.com/alokdidde/uzumaki/issues/2) | Foundation | MVP | 01 |
| 03 | [Bootstrap the package and offline continuous integration](https://github.com/alokdidde/uzumaki/issues/3) | Foundation | MVP | 02 |
| 04 | [Record execution events and support semantic replay](https://github.com/alokdidde/uzumaki/issues/4) | Foundation | MVP | 03 |
| 05 | [Enforce lifecycle budgets cancellation and failure handling](https://github.com/alokdidde/uzumaki/issues/5) | Execution | MVP | 04 |
| 06 | [Isolate generated candidates in a restricted worker](https://github.com/alokdidde/uzumaki/issues/6) | Execution | MVP | 05 |
| 07 | [Implement a versioned skill registry](https://github.com/alokdidde/uzumaki/issues/7) | Execution | MVP | 06 |
| 08 | [Complete an offline task execution baseline](https://github.com/alokdidde/uzumaki/issues/8) | Execution | MVP | 07 |
| 09 | [Add one model provider with separate role contexts](https://github.com/alokdidde/uzumaki/issues/9) | Models | MVP | 08 |
| 10 | [Implement planner skill selection and abstention](https://github.com/alokdidde/uzumaki/issues/10) | Models | MVP | 09 |
| 11 | [Build context assembly and selective memory retrieval](https://github.com/alokdidde/uzumaki/issues/11) | Models | MVP | 10 |
| 12 | [Generate candidate programs or graphs through allowed tools](https://github.com/alokdidde/uzumaki/issues/12) | Models | MVP | 11 |
| 13 | [Implement bounded failure diagnosis and candidate repair](https://github.com/alokdidde/uzumaki/issues/13) | Models | MVP | 12 |
| 14 | [Build the controlled benchmark and comparison runner](https://github.com/alokdidde/uzumaki/issues/14) | Evaluation | MVP | 13 |
| 15 | [Integrate one pinned physics simulator and task family](https://github.com/alokdidde/uzumaki/issues/15) | Evaluation | MVP | 14 |
| 16 | [Evaluate generated policies in simulation](https://github.com/alokdidde/uzumaki/issues/16) | Evaluation | MVP | 15 |
| 17 | [Promote validated skills with rollback and provenance](https://github.com/alokdidde/uzumaki/issues/17) | Evaluation | MVP | 16 |
| 18 | [Complete the MVP demonstration and reproducible release](https://github.com/alokdidde/uzumaki/issues/18) | Evaluation | MVP | 17 |
| 19 | [Accumulate validated repair knowledge across tasks](https://github.com/alokdidde/uzumaki/issues/19) | Learning | Post-MVP | 18 |
| 20 | [Support task decomposition and longer horizon recovery](https://github.com/alokdidde/uzumaki/issues/20) | Learning | Post-MVP | 19 |
| 21 | [Build the outcome-backed model training dataset](https://github.com/alokdidde/uzumaki/issues/21) | Learning | Post-MVP | 20 |
| 22 | [Benchmark a specialized decision model](https://github.com/alokdidde/uzumaki/issues/22) | Learning | Optional | 18, 20, 21 |
| 23 | [Benchmark an owned policy-generation model](https://github.com/alokdidde/uzumaki/issues/23) | Learning | Optional | 18, 21 |
| 24 | [Add graph search and parallel experiment scheduling](https://github.com/alokdidde/uzumaki/issues/24) | Extensions | Optional | 18, 20 |
| 25 | [Evaluate predictive models as optional planning tools](https://github.com/alokdidde/uzumaki/issues/25) | Extensions | Optional | 18, 20 |
| 26 | [Plan guarded hardware execution and physical experiment loops](https://github.com/alokdidde/uzumaki/issues/26) | Extensions | Optional | 18, 20 |

## 01 Freeze the first task and evaluation protocol

Stage: Foundation. Scope: MVP. Prerequisites: None.

Choose one rigid-object pick-and-place task family and freeze its operating envelope before implementation.

Acceptance:

- Record observations, goal, object/pose variation, allowed tools, completion and failure conditions, retry and wall-time budgets, and non-goals.
- Freeze disjoint development and held-out evaluation cases, baseline, metrics, thresholds, seed identities, and evidence retention. Keep evaluator-owned answers out of model context.
- Select one initial simulator and model-provider candidate through a short decision record; Python 3.12 is the proposed harness language, pending this scope decision. Physical robot work is excluded from MVP.

How to test:

- Review ambiguous goals, missing observations, unreachable targets, and partial progress; each must have a named outcome.
- Verify held-out cases cannot enter prompt examples, retrieval, repair feedback, or training data.
- A fresh checkout can inspect all protocol inputs without paid APIs, a simulator, or hardware; documentary checks only at this stage.

## 02 Define versioned harness and model contracts

Stage: Foundation. Scope: MVP. Prerequisites: 01.

Specify the smallest interfaces linking task context, planner, generator, skills, worker, and evaluator.

Acceptance:

- Define task, timestamped observation, eligible-skill list, planner decision/abstention, candidate artifact, execution event, outcome, and model provenance.
- Separate observed facts, inferred judgments, objectives, constraints, hints, and partial physical progress; define schema-version rejection.
- Planner choices cannot directly mutate skills or evaluator policy. Generator outputs are untrusted candidate artifacts, never executable authorization.

How to test:

- Specify fixtures for malformed, missing, stale, contradictory, oversized, and unsupported-version inputs.
- Specify legal lifecycle transitions, cancellation outcomes, and rejection reasons.
- Review that final task-success evidence comes from the environment evaluator, not model self-report.

## 03 Bootstrap the package and offline continuous integration

Stage: Foundation. Scope: MVP. Prerequisites: 02.

Create the minimal Python package, locked environment, test runner, and clean-checkout checks.

Acceptance:

- Pin Python and direct/transitive dependencies; provide one documented offline test command after initial dependency installation.
- Add a recorded/fake model provider and deterministic fixture; default checks require no API keys or paid inference.
- Run formatting, static checks, and unit tests in CI; pin CI actions and exclude secrets, datasets, and raw execution bundles from Git.

How to test:

- Execute documented setup and checks from a fresh checkout.
- Run with model/network access disabled and confirm no live inference occurs.
- Check missing configuration yields a clear error without printing secrets.

## 04 Record execution events and support semantic replay

Stage: Foundation. Scope: MVP. Prerequisites: 03.

Make every experiment attributable before introducing model-driven behavior.

Acceptance:

- Record run/task/candidate IDs, schema and skill/model versions, observation identities, tool calls, decisions, outcomes, and relevant timings.
- Distinguish recorded-decision replay, fresh-model reevaluation, and environment resimulation; never silently substitute one for another.
- Validate event ordering/completeness, sensitive-data redaction, checksums, and bounded storage; declare which artifacts are required for a claim.

How to test:

- Replay recorded decisions with no provider access and compare semantic outcomes.
- Reject truncated, reordered, corrupt, or version-mismatched evidence.
- Verify redaction does not silently destroy required replay inputs; mark such runs non-replayable.

## 05 Enforce lifecycle budgets cancellation and failure handling

Stage: Execution. Scope: MVP. Prerequisites: 04.

Bound the harness loop even when tools or models fail.

Acceptance:

- Enforce wall-time, model-call, token/cost where available, execution-step, retry, and output-size budgets.
- Define task completion, abstention, blocked, timeout, cancellation, failed, and partial-progress states with idempotent transitions.
- Reject late results after cancellation using run/attempt identities; release resources and do not pretend completed work was rolled back.

How to test:

- Inject provider timeout, retry storm, malformed results, worker failure, late completion, and cancellation races.
- Assert finite termination and no extra tool/model calls after terminal state.
- Use a fake clock for logic tests and separate real-process deadline tests.

## 06 Isolate generated candidates in a restricted worker

Stage: Execution. Scope: MVP. Prerequisites: 05.

Establish containment before any model-generated code can execute.

Acceptance:

- Choose and document an OS-enforced worker boundary with denied network, no credentials, restricted filesystem, process-tree cleanup, and CPU/memory/output limits.
- Allow only declared robot/simulator tools through a versioned broker; candidate code cannot write evaluator, prompts, skills, or held-out assets.
- Keep watchdog and kill authority outside the worker; treat schema validation and Python restrictions as insufficient isolation.

How to test:

- Adversarial fixtures attempt file escape, credential access, network access, subprocess escape, infinite loop, memory exhaustion, and output flooding.
- Kill a hung or crashed worker and verify the parent remains responsive and the full process tree is cleaned up.
- Record tested platform and containment limits; unverified platforms fail admission.

## 07 Implement a versioned skill registry

Stage: Execution. Scope: MVP. Prerequisites: 06.

Represent skills as immutable artifacts with explicit applicability and evidence.

Acceptance:

- Store skill ID/version/hash, instructions, input/output contracts, applicability, tool permissions, resource bounds, and completion/failure semantics.
- Keep candidate, validated, deprecated, and rejected states separate; pin running tasks to a version.
- Validate references and reject incompatible dependencies or conflicting skill IDs.

How to test:

- Test missing tools, incompatible versions, duplicate IDs, invalid contracts, and tampered artifacts.
- Verify modifying the registry cannot alter an already pinned execution.
- Load a small hand-authored skill set without any model provider.

## 08 Complete an offline task execution baseline

Stage: Execution. Scope: MVP. Prerequisites: 07.

Demonstrate goal to skill execution to independently verified outcome without model generation.

Acceptance:

- Use a rule-based selector and hand-authored fixture skills for the first task contract.
- Exercise observation, eligibility, bounded execution, retries, verification, and final event bundle end to end.
- Reject safe-but-motionless or success-report-only candidates when the task requires actual progress.

How to test:

- Run nominal, unreachable-goal, failed-grasp, stale-observation, retry-exhausted, cancellation, and worker-crash fixtures.
- Compare semantic replay with fresh fixture execution.
- Require all frozen fixture correctness checks to pass with no paid APIs.

## 09 Add one model provider with separate role contexts

Stage: Models. Scope: MVP. Prerequisites: 08.

Use one underlying model for planning and generation without coupling their state.

Acceptance:

- Implement provider-neutral request/result contracts and one live adapter plus the recorded provider.
- Keep planner and generator prompts/contexts separate; pin model IDs and prompt versions and record usage, retries, latency, and errors.
- Live calls are opt-in, budgeted, redacted, and replaceable; absence of keys does not break offline tests.

How to test:

- Contract-test both roles against recorded valid and malformed responses.
- Inject rate limits, disconnects, provider schema changes, and unknown usage fields.
- Run a small explicit live smoke suite separately from CI; report observed behavior without claiming deterministic model output.

## 10 Implement planner skill selection and abstention

Stage: Models. Scope: MVP. Prerequisites: 09.

Let the model select an eligible existing skill or request more information within the harness state machine.

Acceptance:

- Constrain decisions to explicit eligible IDs, typed parameters, information requests, completion proposals, or abstention.
- Validate every choice against current state, permissions, freshness, and budgets before dispatch.
- Require evaluator confirmation of completion; exclude arbitrary code execution from planner output.

How to test:

- Reject invented skill IDs, invalid parameters, stale choices, and unauthorized requests.
- Test no-applicable-skill, ambiguous task, contradiction, and abstention cases.
- Compare against the rule-based baseline on identical development cases.

## 11 Build context assembly and selective memory retrieval

Stage: Models. Scope: MVP. Prerequisites: 10.

Give each role relevant context without treating retrieved content as instructions.

Acceptance:

- Assemble bounded task state, current observations, recent outcomes, eligible skills, and relevant validated guidance.
- Tag source, age, version, trust, and uncertainty; exclude held-out answers and secrets.
- Define truncation/summarization and freshness behavior; retain references to original evidence and preserve decision-critical constraints.

How to test:

- Inject misleading memory, prompt injection in tool output, stale observations, oversized history, and conflicting guidance.
- Verify token/context budgets preserve required constraints or force abstention.
- Test retrieval never exposes held-out evaluation contents to learning or generation.

## 12 Generate candidate programs or graphs through allowed tools

Stage: Models. Scope: MVP. Prerequisites: 11.

Introduce policy generation as artifact creation, not unrestricted execution.

Acceptance:

- Choose one MVP skill representation through a decision record; do not build competing full DSLs.
- Generate immutable candidates referencing allowed tools and typed inputs/outputs; retain task, prompt, and model provenance.
- Run syntax/schema, dependency, connection, and permission checks before submission to the restricted worker.

How to test:

- Reject dangling graph references, incompatible ports, unknown tools, undeclared dependencies, oversized artifacts, and invalid syntax.
- Demonstrate rejected artifacts never reach worker execution.
- Evaluate valid and semantically incorrect candidates against the independent fixture oracle.

## 13 Implement bounded failure diagnosis and candidate repair

Stage: Models. Scope: MVP. Prerequisites: 12.

Use trace evidence to revise failed candidates without changing the experiment rules.

Acceptance:

- Provide relevant call inputs/outputs, return reasons, state changes, and evidence references to the generator.
- Maintain parent/child candidate lineage and fixed attempt, execution, and inference budgets.
- The repair loop cannot change evaluator definitions, held-out cases, sandbox restrictions, or acceptance thresholds.

How to test:

- Use failures from perception, planning, execution, and verification; check repair feedback identifies the failed stage.
- Detect repeated identical repairs and budget exhaustion without infinite loops.
- Test attempts to modify test expectations, read hidden answers, or claim unsupported success are rejected.

## 14 Build the controlled benchmark and comparison runner

Stage: Evaluation. Scope: MVP. Prerequisites: 13.

Compare task outcomes fairly before adding richer execution environments.

Acceptance:

- Run the frozen development/held-out split with fixed seeds, case identities, budgets, and independently measured success.
- Separate invariant checks and same-artifact replay from task performance; improved candidates may use different trajectories.
- Report success with denominators/uncertainty, retry allowance, interventions, failure categories, latency tails, calls/tokens/cost, and time to accepted candidate.

How to test:

- Recompute reports from retained raw events and reject missing evidence.
- Verify model variants see equivalent observations/tools/budgets and no held-out feedback leaks into repairs.
- Test aggregation with failures, timeouts, zero trials, unavailable usage, and incomplete runs; unavailable metrics remain unavailable.

## 15 Integrate one pinned physics simulator and task family

Stage: Evaluation. Scope: MVP. Prerequisites: 14.

Move beyond the deterministic fixture using the simulator selected in issue 01.

Acceptance:

- Pin simulator/model/assets and expose observation, execution, reset, readiness verification, and task-outcome adapters.
- Capture diagnostic frames/overlays and per-tool traces where applicable; evaluator uses environment state independently of model claims.
- Declare pose/object variation and sim limitations; do not claim physical-robot equivalence or bit-identical physics across platforms.

How to test:

- Verify reset success/failure, impossible goals, collision/contact failures, and changed task instances.
- Run hand-authored baseline skills on the frozen simulator cases.
- Keep simulator integration tests opt-in with explicit dependency checks; requested-but-unavailable simulation fails rather than silently skipping.

## 16 Evaluate generated policies in simulation

Stage: Evaluation. Scope: MVP. Prerequisites: 15.

Demonstrate generation and repair against actual simulated task outcomes.

Acceptance:

- Execute candidate programs/graphs through the restricted worker and simulator broker.
- Rehearse across development variations, preserving per-instance outcomes and candidate lineage.
- Evaluate frozen candidates on held-out cases without additional repair or context updates.

How to test:

- Compare hand-authored/rule baseline, model planning with existing skills, and generation-plus-repair under the same frozen protocol.
- Include grasp failure, wrong placement, geometry variation, and verifier disagreement cases.
- Require task-specific acceptance thresholds from issue 01 and zero tolerated invariant violations.

## 17 Promote validated skills with rollback and provenance

Stage: Evaluation. Scope: MVP. Prerequisites: 16.

Make candidate acceptance explicit and reversible without rewriting execution history.

Acceptance:

- Require structural, containment, behavioral, and held-out evidence before promotion; block self-approved model promotions.
- Publish immutable versioned skill artifacts and applicable environment/tool bounds with evidence references.
- Support deprecation/rollback for future selections while active runs stay pinned; no claim that rollback reverses completed physical work.

How to test:

- Attempt promotion with missing, failed, mismatched, stale, or tampered evidence.
- Verify failed/partial candidates never appear in eligible validated skills.
- Test rollback and deprecation during a pinned execution and audit resulting events.

## 18 Complete the MVP demonstration and reproducible release

Stage: Evaluation. Scope: MVP. Prerequisites: 17.

Deliver a reviewable end-to-end demonstration of the complete initial harness.

Acceptance:

- Demonstrate existing-skill selection and missing-skill generation/repair/promotion for the first task family.
- Provide clean-checkout fixture, simulator, and optional live-model commands with locked versions and retained results.
- Publish limitations, baseline comparison, cost/latency measurements, and a tagged internal release; no physical deployment claims.

How to test:

- Run the full offline suite, worker containment suite, simulator integration, held-out evaluation, and replay on the release revision.
- Have a reviewer reproduce the documented demo and check raw evidence against reported results.
- Ensure missing credentials do not block fixture tests and no secrets/raw private data enter Git or logs.

## 19 Accumulate validated repair knowledge across tasks

Stage: Learning. Scope: Post-MVP. Prerequisites: 18.

Add ASPIRE-style reusable guidance after basic candidate promotion works.

Acceptance:

- Store failure signature, applicability, validated repair strategy, optional code sketch, and evidence/version links.
- Deduplicate, invalidate, and deprecate stale guidance; keep guidance separate from executable skill artifacts.
- Promote cross-task knowledge only after validation on multiple development variations; freeze memory during held-out evaluation.

How to test:

- Compare matched-budget runs with and without memory on unseen task instances.
- Inject stale, conflicting, overly specific, and malicious entries and verify safe rejection or abstention.
- Measure transfer success and inference/repair cost without attributing gains to leaked held-out feedback.

## 20 Support task decomposition and longer horizon recovery

Stage: Learning. Scope: Post-MVP. Prerequisites: 19.

Let the planner coordinate multiple validated skills and request new capabilities when needed.

Acceptance:

- Represent subgoals, dependencies, progress, and resumable boundaries explicitly.
- Handle interruptions, partial completion, new observations, and goal changes without silently replaying completed actions.
- Bound replan depth, repetitions, and total task budget; provide escalation when no valid continuation exists.

How to test:

- Evaluate held-out multi-step tasks and compare against fixed workflow baselines.
- Inject mid-task failure, context loss, stale completion, duplicate events, and unreachable subgoals.
- Check no hidden assumption of general open-world competence beyond demonstrated task families.

## 21 Build the outcome-backed model training dataset

Stage: Learning. Scope: Post-MVP. Prerequisites: 20.

Create trustworthy data for an owned selector or generator before choosing a training approach.

Acceptance:

- Export task/context snapshots, eligible choices, generated artifacts, repairs, versions, outcomes, and evidence identities.
- Separate observations from labels and teacher judgments; chosen-action success does not label untried alternatives as worse.
- Check source/provider terms and consent, redact sensitive content, version datasets, and split by task family/scene/object to limit leakage.

How to test:

- Audit lineage and reproduce labels from independent outcomes; quarantine unverifiable examples.
- Check train/validation/test overlap, duplicate trajectories, missing outcomes, class imbalance, and license restrictions.
- Validate exports never silently include protected evaluation answers or secrets; preserve both successful and failed attempts.

## 22 Benchmark a specialized decision model

Stage: Learning. Scope: Optional. Prerequisites: 18, 20, 21.

Test a Jev-style bounded selector only if quality, cost, latency, or deployment needs justify it.

Acceptance:

- Compare rules, the existing general model, an optional hosted decision model, and a small owned selector trained/adapted on permitted data.
- Return skill rankings/selection and abstention; evaluate calibration on task data without equating confidence with correctness.
- Adopt only if predeclared held-out success/intervention constraints and the selected cost/latency objective pass.

How to test:

- Use identical eligible choices, observations, task budgets, and paired held-out cases.
- Test distribution shifts, unknown skills, ambiguous contexts, option-order sensitivity, and provider failures.
- Measure downstream task success plus decision accuracy, abstention/coverage, calibration, and latency tails.

## 23 Benchmark an owned policy-generation model

Stage: Learning. Scope: Optional. Prerequisites: 18, 21.

Evaluate generator fine-tuning or adaptation from validated artifacts and repair episodes.

Acceptance:

- Choose a permitted base model and supervised/adaptation recipe; freeze dataset, compute budget, baseline, and artifact representation.
- Retain failed candidates and edits with independently verified outcomes; do not treat synthetic teacher answers as physical ground truth.
- Compare against the unchanged general-model generator and adopt only on held-out acceptance, generalization, and cost evidence.

How to test:

- Hold out tasks/scenes/objects and check memorization of evaluator answers or exact programs.
- Run structural validity, sandbox, behavioral success, repair-efficiency, and full harness regression evaluation.
- Reproduce training/evaluation with dataset/model hashes and record compute/hardware; do not claim to reproduce proprietary Jev training.

## 24 Add graph search and parallel experiment scheduling

Stage: Extensions. Scope: Optional. Prerequisites: 18, 20.

Scale candidate exploration only after the serial loop and evidence ownership are reliable.

Acceptance:

- Search a bounded population of graph/program variants with explicit experiment budgets and independent workers.
- Version simulator instances, reserve resources, fence cancellation, and isolate evidence and evaluator state per attempt.
- Compare serial and parallel search by total compute/cost as well as elapsed time; retain unsuccessful branches.

How to test:

- Inject worker failure, duplicate results, budget contention, cancellation races, and cross-run data contamination.
- Verify no resource oversubscription or lost evidence and all attempts are accounted for.
- Benchmark matched-budget search on held-out task families and report speed/cost tradeoffs.

## 25 Evaluate predictive models as optional planning tools

Stage: Extensions. Scope: Optional. Prerequisites: 18, 20.

Investigate world models only if measured failures reveal a prediction bottleneck.

Acceptance:

- Define one prediction-dependent planning question and compare with existing simulator/planner tools.
- Use action-conditioned predictions where evaluating action effects; predictions remain uncertain inputs, not outcome evidence.
- Freeze transfer/OOD tests and an adoption threshold tied to real task performance and inference cost.

How to test:

- Test multi-step error accumulation, unseen objects/actions, and model-exploitation failures.
- Compare against no-world-model and simulator-assisted baselines with identical budgets.
- Require independently observed execution results; visual plausibility or latent prediction loss alone cannot establish success.

## 26 Plan guarded hardware execution and physical experiment loops

Stage: Extensions. Scope: Optional. Prerequisites: 18, 20.

Extend the harness to real experiments only through a separately approved deployment profile.

Acceptance:

- Define one robot, tools, calibration, physical protection, operator controls, reset verification, and success detector; simulator resets are not physical resets.
- Keep protection effective when model/network/harness fail; define allowed interventions, partial progress, and stop/recovery behavior.
- Freeze an operator-approved guarded test protocol before powered work; autonomy/fleet scaling needs new evidence.

How to test:

- Start with mock/disabled-actuation transport tests, then fault injection for loss of connectivity, stale state, harness stall/crash, and reset failure.
- Run guarded physical trials only after profile prerequisites are evidenced and authorized.
- Report per-attempt and retry-conditioned success, interventions, reset reliability, utilization, and full-sequence completion with denominators.

## Definition of done

An issue closes only after its frozen acceptance checks pass on the final revision, failure paths are exercised, exact reproduction commands and retained evidence are linked, and review findings are resolved. Planning this work does not pass its tests. Optional research requires an explicit adoption decision; a negative result is valid evidence.
