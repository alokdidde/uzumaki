# 0002: SO-101 arm in MuJoCo, with an SO-101 case set

Status: **candidate_for_review**. It adds an embodiment and a development case set. It does not change `pick-place-v1`'s rules, tool contracts, budgets, completion thresholds, acceptance targets or held-out commitments.

## Context

The project goal moved to a visible end-to-end demo sooner, so a slice of roadmap feature 15 (one pinned physics simulator) was pulled ahead of features 09 to 14. The owner chose MuJoCo, as already proposed in [0001](0001-task-and-evaluation.md), and the Robot Studio SO-101 arm as the demo robot. `pick-place-v1` never named a robot.

## Finding: the frozen case ranges do not fit the SO-101

These figures were measured with the pinned model (MuJoCo 3.14.0, Menagerie `robotstudio_so101` at `cda5236f`) by damped-least-squares inverse kinematics over sampled targets:

- Top-down grasps (fingers vertical, any jaw yaw) reach horizontal radii of 0.09 to 0.32 m from the base at 0.012 m height, 0.10 to 0.31 m at 0.03 m, and 0.15 to 0.26 m at 0.08 m. Nothing is reachable at 0.12 m. Pan is limited to ±110°.
- The frozen `pick-place-v1` ranges (initial x in [-0.15, -0.05], target x in [0.05, 0.15], y in [-0.15, 0.15]) need radii from about 0.05 m to more than 0.36 m from any single base position on or beside the table. At the base positions tried (centre, and 0.12 to 0.20 m behind the centre facing +y), 53 to 73 percent of grid targets were unreachable.

## Decision

- **Robot and placement:** the SO-101 base is at tabletop (0, -0.20) facing +y. Assets are fetched from the pinned Menagerie commit and verified by SHA-256 on every load (`uzumaki.sim.assets`). Nothing is vendored into Git.
- **Case set `so101-dev-v1`:** the same SHA-256 seed recipe as `pick-place-v1`, with geometry drawn in polar coordinates around the base: box size [0.03, 0.06] m, radius [0.15, 0.23] m, bearing [25°, 60°] off the heading (initial on the -x side, target on the +x side) and yaw [-45°, 45°]. Seeds are `so101-1001` to `so101-1028`. The first eight cases carry the faults and expectations of the eight fixture baseline cases (the unreachable goal stays at (0.40, 0.00)). The other twenty are nominal.
- **No SO-101 held-out set yet.** Held-out seeds must be generated and kept by the protocol custodian (`alokdidde`), never by a coding worker. Until they exist, no acceptance claim can be made for the SO-101.

## Consequences

- The fixture baseline and `pick-place-v1` stay exactly as they were. The SO-101 cases use the same case format, task protocol ID, skills and harness.
- Results on `so101-dev-v1` are development evidence for a simulated arm only. They are not held-out acceptance, physical-robot evidence or a claim about the original `pick-place-v1` geometry.
