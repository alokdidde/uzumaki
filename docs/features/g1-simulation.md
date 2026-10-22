# MuJoCo Unitree G1 humanoid on a stand

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_g1.py` (part of `make sim` and the CI simulator job). It is the first humanoid on the harness: a Unitree G1 with Dex3 hands stands at a table with its pelvis on a stand and uses its left hand to run the same `pick-place-v1` task as the SO-101, the Panda and Spot, on the original frozen geometry.

![The G1 humanoid at a table, pinching a box with its left hand and placing it on the target](../media/g1-nominal.gif)

The [failed-grasp case](../media/g1-failed-grasp.gif) shows recovery: an injected fault makes the first grasp close above the box, the hand reports it holds nothing, and the next attempt picks it up and completes.

## What was added

- **The robot.** `uzumaki.sim.g1` describes the G1 as an `ArmRig` on the shared arm engine. The embodiment `g1` is a `Humanoid` description: a three-joint torso, two seven-joint arms, two seven-joint Dex3 hands and two six-joint legs. For this task the "arm" is the waist plus the left arm (ten joints), and the "gripper" is the left hand.
- **On a stand.** The model's floating base joint is removed, so the pelvis is fixed at standing height with the feet 1 cm off the floor, and the legs hold the model's standing pose. The table is 0.75 m high (dining-table height, at the G1's hips). The right arm is drawn back with its elbow bent, so it stays clear of the table however the waist turns.
- **The hand.** The Dex3 hand has a thumb (three joints) and two fingers (two joints each).
  - **One grip value.** `G1World` drives all seven joints from one grip value (a synergy). Open, the thumb and the fingertips stand about 9 cm apart. Closed, they cross, so they meet on any box from 3 to 6 cm.
  - **Two fingers as one jaw.** The middle finger is coupled to the index finger, so the two move as one. A flat pad spans both fingertips and another sits on the thumb tip. Without the finger pad, small boxes slipped through the gap between the two fingers.
  - **Tilted grip frame.** The thumb hangs lower than the fingertips. So the rig's `left_tcp` frame is tilted 20° toward the fingers, and a top-down grasp comes down with the hand rolled onto its thumb side, where the thumb and fingertips meet at one height.
  - **Balanced torques.** The model's finger servos are unlimited. Here the thumb joints are limited to 2 N m and the finger joints to 0.5 N m, so the thumb balances the two coupled fingers and the box stays where it is while the hand closes. The hand holds something when the thumb's load is above half its limit.
- **Planning.** Before every plan, the planning model copies the pose the physics holds (as for Spot). The wrist's roll and yaw link meshes overlap at wrist angles the joints allow, so that pair is excluded from collision.

The harness core, the evaluator, the driver, the skills, the evidence format and the shared arm engine did not change.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/g1_cases_v1.json` (case set `g1-dev-v1`), rebuilt by `uzumaki.sim.g1.build_case_set` and checked by a test. These are the eight frozen baseline fault cases exactly as they are, then one nominal case on each of the protocol's twenty development geometries `dev-001` to `dev-020`. This is the same construction as the Panda's and Spot's.
- **Environment:**
  - **Model:** `unitree_g1/g1_with_hands.xml` from MuJoCo Menagerie at commit `cda5236f` (51 files with its BSD-3-Clause LICENSE, SHA-256 pinned; `make sim-assets`).
  - **Placement:** the pelvis stands at (0, -0.38), facing +y, 0.80 m above the floor; the tabletop is 0.75 m above the floor.
  - **Physics:** timestep 5 ms.
- **Correctness guards:** the same as for the SO-101.
  - Frozen expectations are met, including how many `pick` and `place` calls were admitted.
  - There is no forbidden contact.
  - Replaying the recorded decisions gives the same outcome.
  - Completed cases end within 0.02 m and 10°.
- **Reproduction:**
  - Tests: `make sim-assets && make sim`.
  - Evidence and videos: `MUJOCO_GL=osmesa make sim-demo-g1` (or `python -m uzumaki.sim --robot g1`).
  - Conformance: `python -m uzumaki.conformance --embodiment g1 --task pick-place`.

## Results on this branch

- **Dev cases:** all 28 met their frozen expectations with no forbidden contact. Completed cases ended within 0.0094 m and 0.8° of the target.
- **Conformance:** the `g1` embodiment passes all eight checks on `pick-place`.
- **Robustness probe** (not frozen, not acceptance): 200 extra geometries drawn with the frozen `pick-place-v1` recipe from seeds `sweep-0` to `sweep-199`. 187 completed with no contact (worst 0.0141 m, 1.0°). The other 13 were refused before any motion as `unreachable_target`: each has its target in the near right corner, across the body from the left hand, where the hand cannot turn the box to the target yaw. None dropped a box or touched anything it should not.

## Limitations

- **Simulated development results only.** The frozen `pick-place-v1` held-out set belongs to the protocol custodian and has not been run on the G1. There is no physical-robot claim.
- **On a stand.** The pelvis is fixed and the legs only hold a pose; the G1 does not balance, step or walk. Standing free needs a learned whole-body balance policy, and walking a locomotion policy. Both are later stages.
- **One hand.** The right hand does not grip yet. A two-handed G1 could run the `handover` pack, as ALOHA does.
- **Reach.** A left hand reaching across the body to the near right corner cannot always turn the box to the target yaw, so some geometries are refused as `unreachable_target` (see the probe).
- **Modelling choices.** The grip synergy, the finger coupling, the pads, the torque limits and the tilted grip frame are modelling choices made here, not values from Unitree.
