# MuJoCo Franka Panda simulation

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_panda.py` (part of `make sim` and the CI simulator job). It is the second arm on the harness and the first one that is not the SO-101. It runs the same rule selector, hand-authored skills, budgets, independent evaluator and evidence bundles as the SO-101, on a seven-joint arm with a symmetric two-finger gripper.

![Franka Panda picking a box and placing it on the blue target](../media/panda-nominal.gif)

The [failed-grasp case](../media/panda-failed-grasp.gif) shows recovery: an injected fault makes the first pick close above the box, the gripper reports it holds nothing, and the second attempt succeeds.

## What changed to add it

Nothing robot-specific was added to the harness, the skills or the evaluator. The SO-101's kinematics, planning, scene and world code became a shared engine (`uzumaki.sim.arm`) driven by an `ArmRig`, a record of everything robot-specific: the `SingleArm` description, the pinned model files, where the base stands, actuators, ready pose, gripper settings, the tool-centre site, gripper pad bodies and inverse-kinematics seeds. `uzumaki.sim.so101` and `uzumaki.sim.panda` are two such rigs. After the refactor all 28 SO-101 cases produce byte-identical evidence bundles.

The Panda rig adds three things the SO-101 did not need, all as rig settings:

- **A tool-centre site.** The Menagerie model has none, so the scene adds `tcp` on the `hand` body between the fingertip pads.
- **Seven joints.** A top-down pose leaves one degree of freedom free; the inverse kinematics pulls the extra freedom gently toward the ready pose (the model's `home` keyframe), which keeps the elbow up.
- **Gravity compensation.** Without it the position actuators sag about 6 mm under the arm's weight and the fingers touch the table. A real Panda's controller compensates gravity, so the rig does too.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/panda_cases_v1.json` (case set `panda-dev-v1`). The Panda reaches top-down grasps over the whole tabletop except a corner strip next to its base, so unlike the SO-101 it runs the frozen `pick-place-v1` geometry unchanged: the eight fixture baseline fault cases exactly as they are, then one nominal case on each of the protocol's twenty development geometries `dev-001` to `dev-020`. `uzumaki.sim.panda.build_case_set` rebuilds the file from those two sources, and a test checks it.
- **Environment:** as for the SO-101, plus the `franka_emika_panda` model from MuJoCo Menagerie at commit `cda5236f` (69 files with its Apache-2.0 LICENSE, SHA-256 pinned; `make sim-assets`). The base stands on a visual pedestal at (0, -0.45), just off the table edge, facing +y. Physics timestep 5 ms.
- **Correctness guards:** the SO-101's, unchanged: frozen expectations, no forbidden contact, recorded-decision replay to the same outcome, completed cases within 0.02 m and 10°.
- **Reproduction:** `make sim-assets && make sim`; videos and evidence with `MUJOCO_GL=osmesa make sim-demo-panda` (or `python -m uzumaki.sim --robot panda`). Conformance: `python -m uzumaki.conformance --embodiment panda`.

## Results on this branch

- All 28 cases met their frozen expectations with no forbidden contact. Completed cases ended within 0.0019 m and 0.2° of the target. A full run takes about 15 s without video.
- The `panda` embodiment passes all eight conformance checks.
- Robustness probe (not frozen, not acceptance): 200 extra geometries drawn with the frozen recipe from seeds `sweep-0` to `sweep-199`. All 200 completed (worst 0.0044 m, 0.32°).

## Limitations

- Simulated development results only. The protocol's held-out geometry stays with its custodian and was not run. No physical-robot claim.
- The Panda is driven with joint position actuators and gravity compensation, not the real robot's torque interface.
- The same planning limits as the SO-101: sampled joint-space paths are collision checked, not the tracked motion.
