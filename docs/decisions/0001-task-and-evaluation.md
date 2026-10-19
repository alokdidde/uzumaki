# 0001: Initial task, simulator, language, and model candidate

Status: **candidate_for_review**, pending PR acceptance. Not accepted; issue 01 is not Done.

## Context

Issue 01 requires one rigid-object pick-and-place task, a simulator and model-provider candidate, and a frozen evaluation envelope before implementation. Details: [protocol README](../protocol/README.md) and [task-v1.json](../protocol/task-v1.json).

## Decision

- **Task:** a single rigid box, tabletop pick-and-place in simulation (`pick-place-v1`). No hardware, one object.
- **Simulator candidate:** MuJoCo 3.14.0. **UNVERIFIED**: it has not been installed, run, or checked against this task. Supplied reference (not fetched here): <https://mujoco.readthedocs.io/en/3.14.0/changelog.html>.
- **Language:** CPython 3.12 family. The exact toolchain lock belongs to feature 03.
- **Runtime model:** `claude-sonnet-5-5` from Anthropic, one model for both planner and generator roles with separate contexts. **UNVERIFIED**: availability, behavior, and cost are untested. Supplied reference (not fetched here): <https://code.claude.com/docs/en/model-config>.

## Revision 3 (pre-acceptance)

`review_revision=3` repairs the draft after review. It states the shared attempt state machine (`attempt_rules`: starting an attempt increments once, repeated observes stay in it, the third attempt may complete, no fourth attempt or model call 21 is admitted, for baseline and candidate alike), adds `evaluation_failure` for reset/readiness failure, crashes and invalid evidence (always kept in the denominator), separates a supported `misgrasp` from a drop (`invariant_violation`), uses first-terminal-event precedence with post-deadline violations recorded as separate evidence, and scopes leakage retirement to pretrial/protected material while exempting ordinary current-trial operational observations. It retains the tabletop frame with constant target yaw 0 (`goal_frame`), the deterministic `rule-selector-v1` baseline and shared skill contracts, and the mandatory invariant set. This is a documentary revision before acceptance, **not a performance result**. The numerical targets and heldout commitments are unchanged; the decision above is unchanged and remains a candidate.

## Alternatives considered

Not exhaustively evaluated. Hardware was excluded as out of MVP scope; multiple objects and a separate decision model were deferred to later features. Other simulators and models were not compared; this is a candidate choice, to be revisited if verification fails.

## Consequences

- Simulator and asset execution, containment, and live provider experiments belong to later issues and need explicit opt-in.
- No paid calls are authorized for Uzumaki by this document.
- No runtime or restricted evaluator exists; nothing here claims one.
- Thresholds in the protocol are unmeasured targets.
- If MuJoCo or the model fails verification, a superseding record and a new protocol version are required rather than editing this one silently.
