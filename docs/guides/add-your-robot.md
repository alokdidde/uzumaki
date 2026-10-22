# Add your robot

A robot joins the harness as a description plus an adapter. You never edit the core. This guide uses the SO-101, which took these exact steps.

## 1. Describe the body

Use the highest layer that fits. Most robots are a ready-made configuration:

```python
from uzumaki.robots import Arm, Gripper, SingleArm

SO101 = SingleArm(
    "so101",
    model="robotstudio_so101/so101.xml",
    arm=Arm(("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")),
    gripper=Gripper(("gripper",), site="gripperframe"),
)
```

Joint and site names are the names in your model file (MJCF or URDF).

| Configuration | Components | Handles |
| --- | --- | --- |
| `SingleArm` | arm, gripper, optional camera | `arm`, `gripper` |
| `TwoArms` | two arms with grippers, optional torso | `left`, `right`, `left_gripper`, `right_gripper` |
| `MobileManipulator` | wheeled base, arm, gripper | `base`, `arm`, `gripper` |
| `Humanoid` | torso, two arms with hands, two legs, head | `left`, `right`, `left_hand`, `right_hand`, `legs`, `head` |
| `Quadruped` | four legs, optional head and camera | `legs`, `head` |
| `QuadrupedWithArm` | quadruped with an arm on its back | adds `arm`, `gripper` |
| `QuadrupedWithJaw` | quadruped that grips with a jaw on its neck | adds `neck`, `jaw` |

A body that fits none of these is assembled from components with `Robot`; any component can mount on another with `on=`:

```python
from uzumaki.robots import Camera, Gripper, Legs, Robot, Torso

hexapod = Robot(
    "hexapod",
    model="hexapod.xml",
    body=Torso(),
    legs=Legs(tuple((f"coxa_{i}", f"femur_{i}", f"tibia_{i}") for i in range(6)), on="body"),
    mandible=Gripper(("mandible",), on="body", kind="jaw"),
    eye=Camera(site="eye", on="body"),
)
print(hexapod.body().summary())
```

`summary()` prints what the body can do and which parts each capability moves. If neither layer fits, write a `uzumaki.body.BodyModel` directly.

## 2. Write the adapter

An adapter (see `uzumaki.embodiments.EnvironmentAdapter`) gives, for a task pack:

- `cases(task)`: the cases your robot can reach. The task's rules and evaluator stay the same.
- `environment(task)`: a factory for a fresh `CaseEnvironment` per case. It holds a clock, a world that implements the pack's tools and an evaluator that reads only privileged state.
- `model_names()`: the joints and sites in the model file, so conformance can check your description.
- `identity()`: versions and hashes that pin a run.

The adapter also enforces your robot's physical limits and safety envelope. Skills are never trusted with them. The SO-101's adapter is `uzumaki/sim/adapter.py`; its world, kinematics and scene are in `uzumaki/sim/`.

**A single arm in MuJoCo needs no adapter code.** Write an `ArmRig` (`uzumaki/sim/arm.py`) and add it to `RIGS`; the shared engine supplies the scene, kinematics, world, evaluator and adapter (`ArmAdapter`). The Panda took one file, `uzumaki/sim/panda.py`, about 100 lines: its pinned model, base placement, actuators, gripper control values, a tool-centre site (added to the model when it has none), finger pad bodies, IK seeds and, because it is heavy, gravity compensation. See [Panda simulation](../features/panda-simulation.md).

## 3. Register it

Add one entry to `EMBODIMENTS` in `uzumaki/embodiments.py`: the name, the robot, the backend, the task packs, and `"module:Factory"` for the adapter. The adapter module is imported only when the embodiment is used, so its dependencies never reach the core.

## 4. Check conformance

```
python -m uzumaki.conformance --embodiment so101 --task pick-place
```

| Check | What it proves |
| --- | --- |
| `body` | the description compiles to a valid body |
| `model_names` | every joint and site exists in the model file |
| `task_binding` | the body has an effector with what the task needs, and every tool maps to a capability |
| `skill_catalog` | the pack's skills only use tools this robot has |
| `stop_preempts` | `stop` takes every part |
| `deterministic_reset` | a case run twice gives identical evidence |
| `replay` | recorded-decision replay reproduces the evidence |
| `evaluator_outcome` | the outcome comes from the independent evaluator |

A robot that passes is supported on that task. Passing says the harness can drive it, not how well it performs.

## 5. Run

The model planner, skill generation and the end-to-end demo then run on your robot like on the SO-101. Pick the model with `--provider` and `--model` ([model backends](../features/model-backends.md)).
