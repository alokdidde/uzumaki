# MuJoCo Unitree Go1 with a jaw on its neck

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_go1.py` (part of `make sim` and the CI simulator job). It is the first `QuadrupedWithJaw` on the harness: a Unitree Go1 dog stands at a low table and grips with a jaw on the end of its neck, running the same `pick-place-v1` task as the SO-101, the Panda, Spot and the G1, on the original frozen geometry.

![The Go1 standing at a low table, picking a box up with the jaw on its neck and placing it on the target](../media/go1-nominal.gif)

The [failed-grasp case](../media/go1-failed-grasp.gif) shows recovery: an injected fault makes the first grasp close above the box, the jaw reports it holds nothing, and the next attempt picks it up and completes.

## What was added

- **The robot.** `uzumaki.sim.go1` describes the Go1 as an `ArmRig` on the shared arm engine. The embodiment `go1` is a `QuadrupedWithJaw` description: twelve leg joints, a six-joint neck and a two-finger jaw.
- **Standing, not fixed.** As with Spot, the trunk keeps its free joint, the twelve leg actuators hold the model's `home` stance, and the body sways a few millimetres when the neck moves. Before every plan the planning model copies the pose the physics holds. Only the lower legs may touch the floor.
- **A neck and a jaw the model does not have.** The Go1 model has neither, so the rig's `customize` builds them before the scene compiles.
  - **The neck.** Six joints on the trunk's top front: yaw, pitch, a second pitch half way along, then the head's pitch, turn and roll. Links of 0.28 m and 0.26 m, 0.3 kg each, with stiff position servos (1000 N m/rad, 30 N m limit). Gravity compensation through the servos carries the neck's weight, so the legs still bear it. Five joints are enough on a fixed base, but with the trunk tilted a degree they cannot hold the jaw straight down at every yaw, so the head has a sixth.
  - **The jaw.** The UMI gripper from MuJoCo Menagerie, a second pinned model, is attached to the head with `MjSpec.attach`. Its six free-floating positioning joints are removed. Its two fingers slide toward each other on one tendon, and the jaw holds something when the closed fingers stop short.
- **Placement.** The table is 0.2 m high, at the dog's shoulder. The trunk stands at (0, -0.52), facing +y, behind the table's edge.
- **Physics settings.** The scene uses MuJoCo's implicit-fast integrator. With the default integrator, the stiff neck servos lagged their targets by about a centimetre at the jaw, and the fingers struck the table.

The harness core, the evaluator, the driver, the skills, the evidence format and the shared arm engine did not change.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/go1_cases_v1.json` (case set `go1-dev-v1`), rebuilt by `uzumaki.sim.go1.build_case_set` and checked by a test. These are the eight frozen baseline fault cases exactly as they are, then one nominal case on each of the protocol's twenty development geometries `dev-001` to `dev-020`. This is the same construction as the Panda's, Spot's and the G1's.
- **Environment:**
  - **Models:** `unitree_go1/go1.xml` (7 files with its BSD-3-Clause LICENSE) and `umi_gripper/umi_gripper.xml` (10 files with its MIT LICENSE) from MuJoCo Menagerie at commit `cda5236f`, SHA-256 pinned (`make sim-assets`).
  - **Physics:** timestep 5 ms.
- **Correctness guards:** the same as for the SO-101.
  - Frozen expectations are met, including how many `pick` and `place` calls were admitted.
  - There is no forbidden contact.
  - Replaying the recorded decisions gives the same outcome.
  - Completed cases end within 0.02 m and 10°.
- **Reproduction:**
  - Tests: `make sim-assets && make sim`.
  - Evidence and videos: `MUJOCO_GL=osmesa make sim-demo-go1` (or `python -m uzumaki.sim --robot go1`).
  - Conformance: `python -m uzumaki.conformance --embodiment go1 --task pick-place`.

## Results on this branch

- **Dev cases:** all 28 met their frozen expectations with no forbidden contact. Completed cases ended within 0.0023 m and 0.7° of the target.
- **Conformance:** the `go1` embodiment passes all eight checks on `pick-place`.
- **Robustness probe** (not frozen, not acceptance): 200 extra geometries drawn with the frozen `pick-place-v1` recipe from seeds `sweep-0` to `sweep-199`. All 200 completed with no contact (worst 0.0033 m, 0.5°).

## Limitations

- **Simulated development results only.** The frozen `pick-place-v1` held-out set belongs to the protocol custodian and has not been run on the Go1. There is no physical-robot claim.
- **Standing only.** The legs hold one stance; the Go1 does not walk. Walking is the next stage, with a learned locomotion policy.
- **A modelled neck and jaw.** No Go1 has this neck. Its geometry, masses and servos, the jaw mount and the integrator are modelling choices made here. The result shows that a jaw on a neck fits the harness, not what a real Go1 could carry. The UMI gripper is a hand-held data-collection gripper, used here as a convenient pinned jaw model.
