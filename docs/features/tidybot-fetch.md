# MuJoCo TidyBot fetch

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_tidybot.py` (part of `make sim` and the CI simulator job) and offline by `tests/test_fetch.py`. It is the first robot on the harness that moves its body: a Stanford TidyBot (a holonomic base with a Kinova Gen3 arm and a Robotiq 2F-85 gripper) drives to a box on one table, picks it up, drives to a second table and places it on the target.

![TidyBot driving to a box, picking it up, driving to the other table and placing it](../media/tidybot-nominal.gif)

The [stopped-short case](../media/tidybot-stopped-short.gif) shows recovery: an injected fault stops the first drive partway, the observation says nothing is within reach, and the next attempt drives again and completes.

## What was added

- **A task pack, `fetch`** (`uzumaki.tasks.FETCH`, protocol `fetch-v1`). It needs `reach` and `grasp`, and also `locomote`, so it binds only to a body that can move: `go_to` claims the base (or a quadruped's legs), `pick` and `place` the arm. Its tools are `observe`, `go_to`, `pick`, `place` and `abort`. The observation adds `base_pose` (room frame) and `within_reach` (`object`, `target`); the case fault `stopped_short_drives` is new. Budgets, outcomes, the evaluator contract and evidence are `pick-place-v1`'s, with `go_to` counted as actuation.
- **Skills and a selector.** `tests/fixtures/skills_fetch_v1.json` holds `go-to`, `pick-box` and `place-box`; `fetch-selector-v1` (`uzumaki.baseline.FetchSelector`) goes to whatever is out of reach and otherwise picks or places. The model planner gets a task card (`uzumaki.planner.FETCH_CARD`).
- **A scripted world for offline tests**, `uzumaki.fetch_env`: two tables, `a` at (0, 0) and `b` at (0.8, 0), each the protocol's 0.5 m tabletop, worked from its -y edge. `go_to` moves the body to the station in front of the table under a point and rejects a point on no table as `unreachable_target`.
- **The robot.** `uzumaki.sim.tidybot` drives the Menagerie TidyBot on the shared arm engine (`ArmRig`), with the base's planar joints moved by its position actuators. `TidybotWorld` lifts the arm to a travel pose before each drive, checks the swept base path against the tables, and plans the place from the current pose (over, approach, release) so the box never sweeps through a table. The embodiment `tidybot` is a `MobileManipulator` description.
- The arm engine gives an unlimited joint a range of ±π for inverse kinematics (the Gen3's continuous joints were clipped to zero before). Earlier robots have no unlimited joints, so their results are unchanged.

The harness core, the evaluator and the evidence format did not change.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/tidybot_cases_v1.json` (case set `tidybot-dev-v1`), rebuilt by `uzumaki.sim.tidybot.build_case_set` and checked by a test. Eight fault cases (`nominal`, `unreachable-goal`, `failed-grasp`, `stopped-short`, `stale-observation`, `retry-exhausted`, `cancellation`, `worker-crash`) then twenty nominal cases. Geometry comes from a seed recipe (SHA-256 of `tidybot-1001` to `tidybot-1028`): a cube of 3 to 6 cm starts on table `a` (x from -0.15 to 0.15 m, y from -0.2 to 0.12 m, yaw within 45°) and its target is on table `b` (x from 0.65 to 0.95 m, same y range). The unreachable-goal case puts the target at (0.4, 0), between the tables. The body starts at (0.4, -0.52) facing +y, at neither table.
- **Environment:** `stanford_tidybot` from MuJoCo Menagerie at commit `cda5236f` (22 files, SHA-256 pinned, with its license and the Kinova Gen3 and Robotiq 2F-85 licenses; `make sim-assets`). Tables 0.45 m high on a floor. Gravity compensation on the arm. Physics timestep 5 ms.
- **Correctness guards:** as for the SO-101: frozen expectations including how many `go_to`, `pick` and `place` calls were admitted, no forbidden contact (the box may touch a table only on its top), recorded-decision replay to the same outcome, completed cases within 0.02 m and 10°.
- **Reproduction:** `make sim-assets && make sim`; evidence and videos with `MUJOCO_GL=osmesa make sim-demo-tidybot` (or `python -m uzumaki.sim --robot tidybot`). Conformance: `python -m uzumaki.conformance --embodiment tidybot --task fetch`.

## Results on this branch

- All 28 cases met their frozen expectations with no forbidden contact. Completed cases ended within 0.0098 m and 3.5° of the target.
- The `tidybot` embodiment passes all eight conformance checks on `fetch`.
- The offline scripted world meets the same 28 expectations and replays.
- Robustness probe (not frozen, not acceptance): 200 extra geometries from the same recipe on seeds `sweep-0` to `sweep-199`. 197 completed as expected with no contact (worst 0.0121 m, 7.0°). Two (`sweep-44`, `sweep-154`) ended as `collision`: the box turned in the grip while the wrist rotated and its side touched table `b`. One (`sweep-193`) completed but needed a second attempt. All three start near the far edge of table `a` (y about 0.1 m) at a yaw near -42°.

## Limitations

- Simulated development results only. There is no held-out case set for `fetch-v1` yet; whoever keeps it should draw it from seeds not in this file. No physical-robot claim.
- The 2F-85 model's grip slips under fast wrist turns, so the carry to the place pose is slow (4 s) and the far, steeply turned corner of the recipe still fails about 1 time in 100 (see the probe above).
- Navigation is a straight drive to a fixed station in front of a table, with a path check against the tables only. There is no map, no obstacle avoidance and no base odometry error.
- The model planner and skill generation are wired for `fetch` but untested on it, offline or live.
- Part claims (`go_to` on the base, `pick` and `place` on the arm) are declared and checked by conformance but not enforced at run time.
