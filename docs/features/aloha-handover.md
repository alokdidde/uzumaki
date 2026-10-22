# MuJoCo ALOHA handover

Status: implemented in the optional `uzumaki.sim` package, tested by `tests/test_aloha.py` (part of `make sim` and the CI simulator job) and offline by `tests/test_handover.py`. It is the first robot with two arms on the harness and the first task that needs both: the left arm picks a bar up, hands it to the right arm in mid-air, and the right arm places it on the target.

![ALOHA handing a bar from the left arm to the right arm](../media/aloha-nominal.gif)

The [dropped-handover case](../media/aloha-dropped-handover.gif) shows recovery: an injected fault makes the giver let go before the taker has closed, the bar falls on the tabletop, the taker reports it holds nothing, and the next attempt picks the bar up again and completes.

## What was added

- **A task pack, `handover`** (`uzumaki.tasks.HANDOVER`, protocol `handover-v1`). It needs a body with two grasping effectors and binds them to two roles, `giver` and `taker`. Its tools are `observe`, `pick` (giver), `handover` (giver and taker), `place` (taker) and `abort` (both). The observation adds `holder` (`giver`, `taker` or none); the case fault `dropped_handovers` is new. Everything else (budgets, outcomes, the evaluator contract, evidence) is `pick-place-v1`'s.
- **Skills and a selector.** `tests/fixtures/skills_handover_v1.json` holds `pick-box`, `hand-over` and `place-box`; `handover-selector-v1` (`uzumaki.baseline.HandoverSelector`) is the rule selector. The model planner gets a task card for the pack (`uzumaki.planner.HANDOVER_CARD`).
- **A scripted world for offline tests**, `uzumaki.handover_env`, so the protocol and driver are tested without MuJoCo.
- **The robot.** `uzumaki.sim.aloha` adds two `ArmRig`s on the shared engine, one per ViperX 300 S arm, and `AlohaWorld`, which implements the five tools on the physics. The embodiment `aloha` is a `TwoArms` description. The scene and world learned to hold several arms (`others=`), and a gripper's two pads touching each other is no longer counted as self contact; any other contact between the arms still ends the case as `collision`.

The harness core, the evaluator and the evidence format did not change.

## Frozen plan

- **Workload:** the 28 cases in `src/uzumaki/sim/data/aloha_cases_v1.json` (case set `aloha-dev-v1`), rebuilt by `uzumaki.sim.aloha.build_case_set` and checked by a test. Eight fault cases (the SO-101's, with `dropped-handover` in place of `stale-after-pick`) then twenty nominal cases. Geometry comes from a seed recipe like `pick-place-v1`'s (SHA-256 of `aloha-1001` to `aloha-1028`): a bar 0.24 m long with a 2.5 to 4 cm square cross-section starts on the giver's side (x from -0.11 to -0.07 m) and its target is on the taker's side (x from 0.07 to 0.11 m), yaw within 30°. The unreachable-goal case puts the target at (0.40, 0).
- **Environment:** `aloha` from MuJoCo Menagerie at commit `cda5236f` (16 files with its BSD-3-Clause LICENSE, SHA-256 pinned; `make sim-assets`). The arms face each other from x = ±0.469 m on visual pedestals. The handover happens with the bar centre at (0, 0, 0.14). Gravity compensation as on the Panda. Physics timestep 5 ms.
- **Correctness guards:** as for the SO-101: frozen expectations including how many `pick`, `handover` and `place` calls were admitted, no forbidden contact, recorded-decision replay to the same outcome, completed cases within 0.02 m and 10°.
- **Reproduction:** `make sim-assets && make sim`; evidence and videos with `MUJOCO_GL=osmesa make sim-demo-aloha` (or `python -m uzumaki.sim --robot aloha`). Conformance: `python -m uzumaki.conformance --embodiment aloha --task handover`.

## Results on this branch

- All 28 cases met their frozen expectations with no forbidden contact. Completed cases ended within 0.0114 m and 1.8° of the target. A full run takes about 10 s without video.
- The `aloha` embodiment passes all eight conformance checks on `handover`.
- Robustness probe (not frozen, not acceptance): 100 extra geometries from the same recipe on seeds `sweep-0` to `sweep-99`. All 100 completed with no contact (worst 0.0116 m, 1.9°).

The placement error is larger than the Panda's (under 2 mm) because the bar shifts a few millimetres in the taker's grip during the exchange; the world plans the place from the grasp it commanded, not from where the bar ended up.

## Limitations

- Simulated development results only. There is no held-out case set for `handover-v1` yet; whoever keeps it should draw it from seeds not in this file. No physical-robot claim.
- Part claims (`pick` on the left arm, `handover` on both) are declared by the task pack and checked by conformance, but the driver still runs one tool call at a time, so they are not enforced at run time.
- The model planner and skill generation are wired for `handover` but untested on it, offline or live.
- The world chooses jaw orientations so the wrist camera mounts face away from each other at the meeting point; other bar lengths or meeting points may need other choices.
