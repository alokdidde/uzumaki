# MuJoCo Spot with arm, standing

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_spot.py` (part of `make sim` and the CI simulator job). It is the first legged robot on the harness: a Boston Dynamics Spot stands on its four legs beside the table and uses the arm on its back to run the same `pick-place-v1` task as the SO-101 and the Panda, on the original frozen geometry.

![Spot standing beside a table, picking a box up with its arm and placing it on the target](../media/spot-nominal.gif)

The [failed-grasp case](../media/spot-failed-grasp.gif) shows recovery: an injected fault makes the first grasp close above the box, the gripper reports it holds nothing, and the next attempt picks it up and completes.

## What was added

- **The robot.** `uzumaki.sim.spot` describes Spot as an `ArmRig` on the shared arm engine. The embodiment `spot` is a `QuadrupedWithArm` description with twelve leg joints, six arm joints and the gripper.
- **Standing, not fixed.** The body keeps the model's free joint, so nothing holds it to the world. The twelve leg actuators hold the model's `home` stance, and the body sways a few millimetres when the arm moves. Before every plan, `SpotWorld` copies the pose the physics holds into the planning model, so inverse kinematics start from where the shoulder actually is. The lower legs may touch the floor (the model's feet are soft spheres that sink a little). Any other robot contact with the floor or table is still `collision`.
- **The gripper.** Spot's gripper is a fixed lower jaw with a finger that swings onto it. The rig adds a `tcp` site near the jaw tips, with the fixed jaw `fixed_jaw_offset_m` to one side, as on the SO-101.
  - **Flat pads.** The jaw collision meshes are convex hulls of curved parts, which pinched small boxes into a pocket by the hinge. So the box is gripped by two flat rubber pads on the jaw faces, and the meshes still collide with everything else.
  - **Finger torque.** The finger's torque is limited to 5 N m (about 50 N on the box), not the model's 1000 N m.
  - **Holding.** The finger's load reports whether it holds something, as Spot's gripper does.
- **Physics settings.** The arm's weight is carried through its joint actuators by gravity compensation, so the legs still bear it. The joints get rotor inertia (armature 0.01): without it, the light finger and wrist chatter against the box at the 5 ms step.
- **Two hooks on the shared engine.** An `ArmRig` can now carry `customize` (edits to the scene before it compiles) and `world` (its own `SimWorld` subclass). The engine also accepts unlimited joints (TidyBot's change). The SO-101, Panda and ALOHA results are unchanged.

The harness core, the evaluator, the driver, the skills and the evidence format did not change.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/spot_cases_v1.json` (case set `spot-dev-v1`), rebuilt by `uzumaki.sim.spot.build_case_set` and checked by a test. These are the eight frozen baseline fault cases exactly as they are, then one nominal case on each of the protocol's twenty development geometries `dev-001` to `dev-020`. This is the same construction as the Panda's.
- **Environment:**
  - **Model:** `boston_dynamics_spot/spot_arm.xml` from MuJoCo Menagerie at commit `cda5236f` (56 files with its BSD-3-Clause LICENSE, SHA-256 pinned; `make sim-assets`).
  - **Placement:** the table is 0.4 m high. Spot's body stands at (0, -0.74), facing +y, clear of the table.
  - **Physics:** timestep 5 ms.
- **Correctness guards:** the same as for the SO-101.
  - Frozen expectations are met, including how many `pick` and `place` calls were admitted.
  - There is no forbidden contact.
  - Replaying the recorded decisions gives the same outcome.
  - Completed cases end within 0.02 m and 10°.
- **Reproduction:**
  - Tests: `make sim-assets && make sim`.
  - Evidence and videos: `MUJOCO_GL=osmesa make sim-demo-spot` (or `python -m uzumaki.sim --robot spot`).
  - Conformance: `python -m uzumaki.conformance --embodiment spot --task pick-place`.

## Results on this branch

- **Dev cases:** all 28 met their frozen expectations with no forbidden contact. Completed cases ended within 0.0077 m and 2.0° of the target.
- **Conformance:** the `spot` embodiment passes all eight checks on `pick-place`.
- **Robustness probe** (not frozen, not acceptance): 200 extra geometries drawn with the frozen `pick-place-v1` recipe from seeds `sweep-0` to `sweep-199`. All 200 completed with no contact (worst 0.0076 m, 1.5°).

## Limitations

- **Simulated development results only.** The frozen `pick-place-v1` held-out set belongs to the protocol custodian and has not been run on Spot. There is no physical-robot claim.
- **Standing only.** The legs hold one stance; Spot does not walk, step or rebalance. Walking is a later stage that needs a learned locomotion policy.
- **Modelling choices.** The flat jaw pads, the finger torque limit and the joint armature are modelling choices made here, not values from Boston Dynamics.
