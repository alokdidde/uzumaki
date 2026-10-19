# 0001: Initial task, simulator, language, and model candidate

Status: **candidate_for_review**, pending PR acceptance. Not accepted; issue 01 is not Done.

## Context

Issue 01 requires one rigid-object pick-and-place task, a simulator and model-provider candidate, and a frozen evaluation envelope before implementation. Details: [protocol README](../protocol/README.md) and [task-v1.json](../protocol/task-v1.json).

## Decision

- **Task:** a single rigid box, tabletop pick-and-place in simulation (`pick-place-v1`). No hardware, one object.
- **Simulator candidate:** MuJoCo 3.14.0. **UNVERIFIED**: it has not been installed, run, or checked against this task. Supplied reference (not fetched here): <https://mujoco.readthedocs.io/en/3.14.0/changelog.html>.
- **Language:** CPython 3.12 family. The exact toolchain lock belongs to feature 03.
- **Runtime model:** `claude-sonnet-5-5` from Anthropic, one model for both planner and generator roles with separate contexts. **UNVERIFIED**: availability, behavior, and cost are untested. Supplied reference (not fetched here): <https://code.claude.com/docs/en/model-config>.

## Revisions 3 and 4 (pre-acceptance)

`review_revision=4` is a documentary revision before acceptance, not a performance result; zero trials have been run. It adds `acceptance_evidence_rules` (all 40 cases, failures included, need independently verifiable invariant evidence; missing or corrupt evidence makes overall acceptance unavailable/fail even at 32 successes and a gain of 4), tightens `misgrasp` to a box that never lifts and stays continuously supported on the tabletop (any unsupported slip or drop is `invariant_violation`), and counts only actual prohibited admission, execution, effects or access as violations: rejected or fenced requests, including stale, before-first-observe and post-deadline ones, add 0. A before-observe request that is rejected ends `missing_observation`; actually admitted, it is `invariant_violation`. The numerical targets, completion criteria, budgets and heldout commitments are unchanged.

`review_revision=3` repaired the draft after an earlier review. It stated the shared attempt state machine (`attempt_rules`: starting an attempt increments once, repeated observes stay in it, the third attempt may complete, no fourth attempt or model call 21 is admitted, for baseline and candidate alike), added `evaluation_failure` for reset/readiness failure, crashes and invalid evidence (always kept in the denominator), separated a supported `misgrasp` from a drop (`invariant_violation`), used first-terminal-event precedence with post-deadline violations recorded as separate evidence, and scoped leakage retirement to pretrial/protected material while exempting ordinary current-trial operational observations. It retained the tabletop frame with constant target yaw 0 (`goal_frame`), the deterministic `rule-selector-v1` baseline and shared skill contracts, and the mandatory invariant set. This is a documentary revision before acceptance, **not a performance result**. The decision above is unchanged and remains a candidate.

## Alternatives considered

Not exhaustively evaluated. Hardware was excluded as out of MVP scope; multiple objects and a separate decision model were deferred to later features. Other simulators and models were not compared; this is a candidate choice, to be revisited if verification fails.

## Consequences

- Simulator and asset execution, containment, and live provider experiments belong to later issues and need explicit opt-in.
- No paid calls are authorized for Uzumaki by this document.
- No runtime or restricted evaluator exists; nothing here claims one.
- Thresholds in the protocol are unmeasured targets.
- If MuJoCo or the model fails verification, a superseding record and a new protocol version are required rather than editing this one silently.
