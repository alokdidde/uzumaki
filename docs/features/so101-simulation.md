# MuJoCo SO-101 simulation (early slice of feature 15)

Status: implemented as an optional package (`uzumaki.sim`) with its own suite (`tests/test_sim.py`, `make sim`) and CI job. It runs the unchanged issue-08 harness (rule selector, hand-authored skills, bounded execution, evidence and replay) against MuJoCo physics on a Robot Studio SO-101 arm. It was pulled ahead of features 09 to 14 to reach a visible demo sooner; see [decision 0002](../decisions/0002-so101-simulation.md). There is no model in the loop yet.

![SO-101 picking a box and placing it on the blue target](../media/so101-nominal.gif)

The [failed-grasp case](../media/so101-failed-grasp.gif) shows a retry: an injected fault makes the first pick close the gripper above the box, the gripper senses it holds nothing, and the second attempt succeeds.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/so101_cases_v1.json` (`uzumaki.baseline-cases/1.0`, protocol `pick-place-v1`, case set `so101-dev-v1`). The first eight mirror the fixture baseline's faults and expected outcomes. The other twenty are nominal and must complete in one attempt. Skills: `tests/fixtures/skills_pick_place_v1.json`.
- **Environment:** Python 3.12, `requirements-dev.txt` plus `requirements-sim.txt` (MuJoCo 3.14.0, NumPy 2.5.4), and the SO-101 model from MuJoCo Menagerie at commit `cda5236f`, fetched by `make sim-assets` and verified file by file against SHA-256 pins on every load. Physics timestep 5 ms. The case clock is the fixture's integer-millisecond clock, advanced by exactly one physics step at a time.
- **Correctness guards:** every case passes the same `correctness_failures` checks as the fixture baseline (outcome, progress, state, status, attempts, admitted pick/place, zero violations, zero model calls, bundle consistency). It records no forbidden contact and replays (recorded-decision replay) to the same semantic outcome. Completed cases end within 0.02 m and 10° of the target by true state.
- **Containment guards:** admission is inherited unchanged from the fixture world, so the same requests are rejected, in the same order, without effect. Unreachable picks or places are rejected before any motion. Planning sees only tool arguments and the goal, never the true box pose. The evaluator alone reads true state.
- **Metrics/thresholds:** pass/fail per case as above. No success rate is claimed as acceptance.
- **Reproduction:** `python -m pip install -r requirements-dev.txt -r requirements-sim.txt && python -m pip install --no-deps -e .`, then `make sim-assets` and `make sim`. For videos and evidence: `MUJOCO_GL=osmesa make sim-demo` (or `MUJOCO_GL=egl` with a GPU), which needs `ffmpeg`.
- **Raw evidence destination:** `python -m uzumaki.sim --out DIR` writes one bundle per case, `summary.json` (simulator, robot commit, asset manifest hash, case file hash, and per case the outcome, correctness failures, contacts and true final error) and, with `--video`, one MP4 per case. `build/` is git-ignored. Only the two demo GIFs in `docs/media/` are committed.

## Implemented

- **Scene (`uzumaki.sim.scene`):** a 0.5 × 0.5 m tabletop in the protocol goal frame, a floor 0.4 m below it, one box (density 250 kg/m³), a visual-only target marker and the SO-101 at (0, -0.20) facing +y.
- **Kinematics and planning (`uzumaki.sim.so101`):** damped-least-squares inverse kinematics for top-down poses (fingers down, jaw axis at a chosen yaw), from several seeds within joint limits. A grasp puts the fixed jaw 3 mm from the box face and closes the moving jaw onto it. Of the four jaw yaws that fit a square box, the planner keeps the reachable one with the least wrist roll that also allows a vertical place at the goal. Every straight joint-space segment of the plan is sampled for robot self contact and robot/table contact.
- **World (`uzumaki.sim.world.SimWorld`):** a subclass of `FixtureWorld` with the same tool handlers, admission, log and audit. `observe` holds the arm for 100 ms and reports the true pose plus the fixture's fixed estimate offset. `pick` moves from the raised ready pose to hover, descends, closes, lifts, and reads "holding" from the gripper joint position, as a servo would. `place` carries the box, sets it down, opens fully and retreats. `abort` holds position. Injected faults keep their fixture meaning; a misgrasp fault closes the gripper at hover height, which is a real physical miss.
- **Evaluator (`SimEvaluator`):** the fixture evaluator's precedence, plus physics. It records forbidden contacts on every step: robot with table or floor, robot self contact, robot/box contact outside the gripper pads, and box with the floor or with the table outside its bounds. Any of these ends the case as `collision` and counts as a violation. A reset that does not leave the box resting cleanly is `evaluation_failure`. Completion means the box is released, resting on the tabletop, within tolerance and below 0.01 m/s for 0.5 s before the deadline.
- **Runner (`uzumaki.sim.run`, `python -m uzumaki.sim`):** `run_sim_case` is the new `uzumaki.baseline.run_case` with a simulation environment. `run_fixture_case` now uses the same `run_case` with the fixture environment, and its behaviour is unchanged.

## Results on this branch

- All 28 cases met their frozen expectations, with no forbidden contacts. Completed cases ended within 0.0041 m and 0.44° of the target. A full run takes about 17 s without video.
- Robustness probe (not frozen, not acceptance): 200 extra geometries from seeds `sweep-0` to `sweep-199` with the same recipe. 199 completed (worst 0.0053 m, 1.96°). One ended as `collision`: the camera mount touched the upper arm during a carry, because the servo-tracked motion left the collision-checked joint-space path.

## Limitations

- These are simulated development results on an SO-101-specific case set. There is no SO-101 held-out set yet, no physical-robot claim, and no claim about the original `pick-place-v1` geometry, which the SO-101 cannot reach (decision 0002).
- Planning checks sampled joint-space paths, not the tracked motion, so a rare collision is possible (the probe above). Slips and drops of a held box are not separately classified; a box that falls ends as `collision` (floor contact) or fails placement.
- Physics is deterministic on one machine and library build. Bit-identical results across platforms are not claimed.
- Rendering needs an OpenGL backend (`MUJOCO_GL=egl` or `osmesa`) and `ffmpeg`. CI runs the physics suite only, without video.
