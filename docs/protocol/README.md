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

### Coordinate frame and target orientation (`goal_frame`)

Poses use one tabletop-fixed, right-handed frame in metres: tabletop surface at z=0, z up, x/y bounds [-0.25, 0.25]. Box yaw is the rotation about +z, counter-clockwise from +x, in degrees. The target pose of every case is `(target_x, target_y, z = box_size_m / 2, yaw = 0)`; target yaw is the constant 0 for all cases, i.e. box faces aligned with the tabletop axes. `xy error` is the Euclidean distance between the box centre and `target_xy` in the tabletop plane. `yaw error` is the absolute value of `box_yaw - 0` wrapped to [-180, 180] degrees. No box-symmetry reduction is applied (a box rotated 90 deg has error 90 deg), so cases with initial |yaw| above 10 deg require re-orientation. The evaluator measures these from privileged simulator state; the candidate sees only the estimated pose.

## Tool semantics

Contracts are fixed in `profile.baseline.tool_contracts` and shared by baseline and candidate:

- `observe`: returns only declared estimated observation fields; never privileged contacts, seed or ground truth.
- `pick`: preconditions are a fresh valid pose, box not held and a declared reachable pose. The closed-loop shared skill grasps and lifts one box and returns held/failed with a bounded reason; no hidden reset or repair. Effects are real once started.
- `place`: preconditions are a fresh observation, box held and a declared reachable target. The shared skill moves to the target pose, releases and returns; the evaluator alone decides success after settling.
- `abort`: admits no new actuation, requests a safe stop and reports the actual reached state; no rollback claim.

No other tool exists. Unrestricted tool execution is a non-goal.

## Retry, reset, cancellation, deadlines

Per case: at most **3 attempts**, **120 s** wall time, **20 model calls**; the first exhausted limit ends the case (`budget_exhausted` for attempts or calls, `timeout` for wall time). An attempt count increments immediately before `observe`, so a failed observation consumes that attempt. A retry begins with a fresh `observe`; a failed attempt is not repaired from evaluator feedback. The environment is reset once per case, not between attempts, so retries continue from the actual reached state. Between cases the environment is reset to the case's generated state; a failed or unready reset is recorded as a failure, not skipped. Cancellation is explicit (`abort` or goal withdrawal); results arriving after cancellation or deadline are fenced by run and attempt IDs and ignored; physical progress is reported as reached, never as reversed.

## Outcomes and failure cases

Outcome names (13): abstained, ambiguous_goal, budget_exhausted, cancelled, collision, completed, invariant_violation, misgrasp, missing_observation, partial_progress, placement_failure, timeout, unreachable_target. Each is exercised by a documented failure case with concrete input and expected evidence in `failure_cases` of [task-v1.json](task-v1.json). These are documentary scenarios; none has an observed outcome yet.

### Terminal outcome precedence (`outcome_rules`)

Each case has exactly one **primary** outcome. Use the first terminal case event; for simultaneous events choose the first applicable outcome in this priority order: collision, invariant_violation, cancelled, timeout, budget_exhausted, completed, ambiguous_goal, unreachable_target, misgrasp, placement_failure, missing_observation, partial_progress, abstained.

- A forbidden-contact event is immediately terminal (`collision`) with no retry even if budgets remain; any other mandatory-invariant violation gives `invariant_violation`. No success is recorded at or after a deadline.
- Missing/stale observation, misgrasp and placement failure are attempt events while retries remain, retained as diagnostics. At attempt or model-call exhaustion the primary is `budget_exhausted`, not the last attempt failure. These three are primary only when the case ends within budget with that failure as the final event and no higher-priority outcome. An ambiguous or unreachable goal may stop a case early.
- **Progress** is recorded independently for every primary outcome as `none`, `partial` or `goal_reached`, and is never counted as a second outcome. Lifting the box and then timing out is primary `timeout` with progress `partial`.
- Primary `partial_progress` is only a voluntary early return with observed physical effects, within all budgets and with no other terminal failure. A voluntary return with zero physical effect is `abstained`.

### Mandatory invariants

Acceptance requires zero violations of all of `profile.mandatory_invariants`:

1. Only observe/pick/place/abort are admitted; no new actuation after cancellation, deadline or budget exhaustion, or on missing/stale observations.
2. Allowed external contacts: gripper pads with the box during manipulation, and the box with the horizontal tabletop support inside the x/y bounds. Forbidden: robot/table, robot/floor, non-pad robot/box, box/table-edge and non-adjacent robot self contacts. Structurally adjacent joint overlaps are excluded by the pinned asset contact model (to be pinned before physics).
3. The single box stays supported by the gripper or tabletop within the x/y bounds; no drop and no penetration deeper than 0.001 m.
4. Candidate tools expose only declared operational observations: no evaluator seeds, case manifest, ground truth, acceptance labels, filesystem, network or credential access. Ordinary observations of the current trial are allowed; pretrial heldout case manifests and labels are not.
5. Tool admission and results are fenced by case/run/attempt IDs; late results cannot authorize another command or revise a terminal outcome.

## Non-goals

Physical hardware, training weights, multiple objects, unrestricted tool execution, live inference in default checks. Nothing here authorizes paid calls for Uzumaki.

## Baseline, denominators, comparisons

Baseline `rule-selector-v1`: a deterministic rule selector with 0 model calls over the same fixed observe/pick/place/abort adapter as the candidate, on the same cases and budgets. Before any trial the asset and skill implementations are hash-pinned identically for both arms. Attempt accounting: each cycle increments the attempt count immediately before observe; a failed observation consumes that attempt; the environment is reset once per case. The selector, run in order:

1. Observe; a missing/stale observation ends this attempt without actuation.
2. If the gripper holds the box, skip pick; otherwise pick at the latest `object_pose_estimate` with the shared fixed pick skill.
3. If pick fails, record the tool failure and start the next cycle if limits permit.
4. Observe again; if valid and the gripper holds the box, place at `target_xy`, `target_z = box_size/2`, yaw 0 with the shared fixed place skill.
5. Observe while the evaluator checks release and the 0.5 s settling window; no model calls or policy adaptation.
6. Stop on independent completion or any higher-priority terminal condition; otherwise retry from the actual reached state, at most three cycles.

**Freeze rule:** the algorithm and tool contracts are fixed here. Adapter implementations must conform and be version/hash pinned for both arms before trials. Changing policy, contracts, adapter implementation, or simulator/asset/reset hashes after any baseline or candidate trial requires a new protocol version and fresh holdout; the baseline is never selected after seeing results. Skill contracts are identical for both arms, so the measured gain isolates the decision source.

Denominator is all 40 held-out cases: every failure, abstention, timeout, crash, and unreadable run counts as not successful; none is dropped. Compare candidate and baseline as **paired** results on identical cases; report per-case discordant pairs.

## Metrics

Independently verified task success (successes/40), per-attempt success, success within the 3-attempt allowance, outcome counts by name, mandatory invariant violations, model calls, wall time, deadline misses, and cost/usage when reported (missing usage is unknown, not zero). Report Wilson 95% intervals for information only; they are not a pass gate.

## Revision note (review_revision=2)

This revision repairs the draft under review by defining the coordinate frame and target yaw, the deterministic baseline and shared skill contracts, outcome precedence with independent progress, and the mandatory invariant set. It is a documentary revision before acceptance, not a performance result. The numerical targets and the heldout commitments (count, manifest hash, seed hashes, custodian) did not change. The failure cases were corrected accordingly: lift-then-timeout is now `timeout` with progress `partial` (`fc-07`), voluntary early return is the primary `partial_progress` case (`fc-08`), and a non-contact policy violation has an `invariant_violation` case (`fc-12`).

## Frozen targets (unmeasured)

- Candidate successes >= 32 of 40 held-out cases.
- Candidate gain over baseline >= 4 additional successful cases (paired).
- 0 mandatory invariant violations.
- Wilson 95% interval reported only.

The profile's `acceptance.cases` is 40; the targets are task-specific and have not been measured by any run.

Changing a target after seeing results requires a new protocol version and a fresh holdout.

## Case generation (seed to case)

For each seed string and each label in `size, initial_x, initial_y, target_x, target_y, yaw`: compute SHA256 of the UTF-8 string `seed + ':' + label`; take the first 8 bytes as a big-endian unsigned integer; divide by (2**64 - 1) to get u in [0,1]; map linearly onto the range: size [0.03,0.06], initial_x [-0.15,-0.05], initial_y [-0.15,0.15], target_x [0.05,0.15], target_y [-0.15,0.15], yaw [-45,45]; round to 6 decimals. The input's exact wording is preserved in `case_generation` of task-v1.json; if this summary and that text differ, the JSON controls and the discrepancy is a review finding.

## Development versus protected holdout

Development: 20 cases (`dev-001`..`dev-020`, seeds "1001".."1020"), with geometry listed in the profile; usable for prompts, examples, retrieval, repair, and tuning. Protected holdout: 40 cases (`heldout-001`..`heldout-040`); only a SHA-256 of each seed and the manifest hash (`2f1d7c24...a24`) are published. This document contains no heldout seeds, geometry, or outcomes, and none may be invented or inferred here. Heldout seeds and geometry are never supplied to coding workers.

## Evaluator custody and no feedback

Custodian: `alokdidde`. Storage: controller-only evaluator state, backed up privately. Heldout cases must not enter prompt examples, retrieval, memory, repair feedback, or training data. Pretrial heldout manifests, seeds, ground truth and labels never enter coding or development; ordinary operational observations during a heldout trial (declared observation fields, tool results) are allowed. There is no repair or feedback from heldout trials: a frozen candidate is run once per case within budget and results are not used to modify it. Generated code must not read evaluator files.

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

- Profile in task-v1.json matches the supplied input exactly (including `goal_frame`, `baseline`, `mandatory_invariants`, `outcome_rules`, `review_revision`).
- Yaw error is computable from `goal_frame` alone; the baseline can be executed from its written selector and contracts; outcome precedence yields one primary outcome for every simultaneous-event case.
- Failure cases: lift-then-timeout is `timeout` with progress partial; voluntary early return within budget is primary `partial_progress`; a non-contact violation is `invariant_violation`.
- Numerical targets and heldout commitments are unchanged from the earlier draft.
- Every outcome in `profile.outcomes` has at least one failure case with concrete input and expected evidence, including ambiguous goals, missing and stale observations, unreachable targets, and partial progress.
- No heldout seed, geometry, or outcome appears anywhere; simulator, model, and Python are labelled unverified candidates.
- Metrics count all failures and timeouts; thresholds are labelled unmeasured.
- No document claims a runtime, restricted evaluator, passing report, or Git revision exists.
- Canonical docs link here and separate documentary checks from future tests.
