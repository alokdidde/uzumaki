# Task and evaluation protocol: pick-place-v1

Status: **candidate_for_review**, pending PR acceptance. Issue 01 is not Done. This is documentation only: no runtime, simulator, evaluator, or measured result exists. Machine-readable profile: [task-v1.json](task-v1.json) (`profile` is copied verbatim from the supplied protocol input; do not edit it in place, version it). Rationale: [decision record 0001](../decisions/0001-task-and-evaluation.md).

All numerical values below are **frozen, unmeasured, task-specific targets** for this candidate. They are not universal robotics thresholds and no run has achieved them.

## Operating envelope

Task family: one rigid box, tabletop pick-and-place, in simulation. No hardware. The box sits on the tabletop; a single object, a single goal pose. Variation per case: box size 0.03-0.06 m, initial xy x in [-0.15, -0.05] and y in [-0.15, 0.15] m, target xy x in [0.05, 0.15] and y in [-0.15, 0.15] m, initial yaw in [-45, 45] deg. Simulator and asset hashes must be pinned (by issue 15) before any physics trial.

## Observations: freshness and privilege

Fields visible to the candidate: `timestamp`, `object_pose_estimate`, `target_pose`, `gripper_state`, `tool_result`. Pose is an estimate, not ground truth. An observation older than **0.2 s** at the time of an actuating call is stale; the call is rejected with a stale-observation result and the candidate must `observe` again. A missing field is a missing observation, never filled from memory or guessed. Privileged evaluator state (ground-truth pose, contacts, seeds, holdout geometry) is never visible to the candidate (`privileged_evaluator_state_visible_to_candidate: false`). Tool outputs and task text are untrusted data.

## Goal and completion

Goal: move the box to `target_pose`. Completion is **measured independently by the evaluator**, never by model self-report. All must hold:

- xy error <= 0.02 m
- yaw error <= 10 deg
- settled for >= 0.5 s with speed <= 0.01 m/s
- object released (not held by the gripper)
- 0 mandatory invariant violations

A goal with no or conflicting target is `ambiguous_goal`.

## Tool semantics

- `observe`: read-only; returns the fields above with timestamp.
- `pick`: attempt a grasp of the box using a fresh observation; may fail (misgrasp, unreachable, collision); effects are real once started.
- `place`: move held box to the target and release; same failure modes.
- `abort`: stop motion and end the attempt; does not undo physical progress.

No other tool exists. Unrestricted tool execution is a non-goal.

## Retry, reset, cancellation, deadlines

Per case: at most **3 attempts**, **120 s** wall time, **20 model calls**; the first exhausted limit ends the case (`budget_exhausted` or `timeout`). A retry begins with a fresh `observe`; a failed attempt is not repaired from evaluator feedback. Between cases the environment is reset to the case's generated state; a failed or unready reset is recorded as a failure, not skipped. Cancellation is explicit (`abort` or goal withdrawal); results arriving after cancellation or deadline are fenced by run and attempt IDs and ignored; physical progress is reported as reached, never as reversed.

## Outcomes and failure cases

Outcome names: abstained, ambiguous_goal, budget_exhausted, cancelled, collision, completed, misgrasp, missing_observation, partial_progress, placement_failure, timeout, unreachable_target. Each is exercised by a documented failure case with input and expected evidence in `failure_cases` of [task-v1.json](task-v1.json). These are documentary scenarios; none has an observed outcome yet.

## Non-goals

Physical hardware, training weights, multiple objects, unrestricted tool execution, live inference in default checks. Nothing here authorizes paid calls for Uzumaki.

## Baseline, denominators, comparisons

Baseline: hand-authored observe/pick/place skills with rule-based selection, on the same cases and budgets. Denominator is all 40 held-out cases: every failure, abstention, timeout, crash, and unreadable run counts as not successful; none is dropped. Compare candidate and baseline as **paired** results on identical cases; report per-case discordant pairs.

## Metrics

Independently verified task success (successes/40), per-attempt success, success within the 3-attempt allowance, outcome counts by name, mandatory invariant violations, model calls, wall time, deadline misses, and cost/usage when reported (missing usage is unknown, not zero). Report Wilson 95% intervals for information only; they are not a pass gate.

## Frozen targets (unmeasured)

- Candidate successes >= 32 of 40 held-out cases.
- Candidate gain over baseline >= 4 additional successful cases (paired).
- 0 mandatory invariant violations.
- Wilson 95% interval reported only.

Changing a target after seeing results requires a new protocol version and a fresh holdout.

## Case generation (seed to case)

For each seed string and each label in `size, initial_x, initial_y, target_x, target_y, yaw`: compute SHA256 of the UTF-8 string `seed + ':' + label`; take the first 8 bytes as a big-endian unsigned integer; divide by (2**64 - 1) to get u in [0,1]; map linearly onto the range: size [0.03,0.06], initial_x [-0.15,-0.05], initial_y [-0.15,0.15], target_x [0.05,0.15], target_y [-0.15,0.15], yaw [-45,45]; round to 6 decimals. The input's exact wording is preserved in `case_generation` of task-v1.json; if this summary and that text differ, the JSON controls and the discrepancy is a review finding.

## Development versus protected holdout

Development: 20 cases (`dev-001`..`dev-020`, seeds "1001".."1020"), with geometry listed in the profile; usable for prompts, examples, retrieval, repair, and tuning. Protected holdout: 40 cases (`heldout-001`..`heldout-040`); only a SHA-256 of each seed and the manifest hash (`2f1d7c24...a24`) are published. This document contains no heldout seeds, geometry, or outcomes, and none may be invented or inferred here. Heldout seeds and geometry are never supplied to coding workers.

## Evaluator custody and no feedback

Custodian: `alokdidde`. Storage: controller-only evaluator state, backed up privately. Heldout cases must not enter prompt examples, retrieval, memory, repair feedback, or training data. There is no repair or feedback from heldout trials: a frozen candidate is run once per case within budget and results are not used to modify it. Generated code must not read evaluator files.

## Leakage, retirement, versioning

If any heldout seed, geometry, or per-case result reaches a candidate, prompt, retrieval store, or training data, or the evaluator is modified to obtain a pass, the affected cases are retired from the protected holdout, a new protocol version (`pick-place-v2` or later) and fresh holdout are created, and earlier claims are marked void rather than edited. Other protocol changes also create a new version.

## Evidence fields

Per trial: protocol version, case id, Git revision and clean/dirty status, dependency lock, OS/platform, simulator and asset hashes, provider/model/prompt IDs, seed id, exact command, exit status, raw event log, tool results, evaluator measurements (pose, yaw, speed, release, contacts), outcome name, budgets consumed, usage/cost if reported, artifact digests. No such evidence exists yet.

## Retrieval and retention

Raw evidence is retained on controller and artifact volumes, access-controlled, with a retrieval manifest and hashes; large or private bundles are not committed to Git. Loss or redaction that removes replay inputs makes the corresponding replay claim unavailable. No Git revision or report is recorded in this document.

## Documentary commands (offline, no network, no install)

From a clean checkout:

```
git diff --check
python3 -c "import json; d=json.load(open('docs/protocol/task-v1.json')); print(sorted(d), len(d['failure_cases']))"
python3 -c "import json; a=json.load(open('docs/protocol/task-v1.json'))['profile']; b=json.load(open('PROTOCOL_INPUT.json'))['profile']; print(a==b)"  # only where PROTOCOL_INPUT.json is available
python3 /path/to/alok-factory/scripts/protocol_verify.py /path/to/uzumaki
```

The last command is the independent checker, which lives outside this repository and requires both private repositories cloned. No package or CI command is implemented yet; these are documentary checks, not tests.

## Manual review criteria

- Profile in task-v1.json matches the supplied input exactly.
- Every outcome in `profile.outcomes` has at least one failure case with concrete input and expected evidence, including ambiguous goals, missing and stale observations, unreachable targets, and partial progress.
- No heldout seed, geometry, or outcome appears anywhere; simulator, model, and Python are labelled unverified candidates.
- Metrics count all failures and timeouts; thresholds are labelled unmeasured.
- No document claims a runtime, restricted evaluator, passing report, or Git revision exists.
- Canonical docs link here and separate documentary checks from future tests.
