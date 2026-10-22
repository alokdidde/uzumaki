# Decision 0004: describe robots by parts, effectors and capabilities

Status: accepted for implementation (chunk A of the embodiment design).

## Context

The harness core (budgets, registry, evidence, sandbox, planner, generator, promotion, model backends) never needed to know which robot it drives. It did, however, assume one arm doing tabletop pick-and-place, and nothing described a robot in a form a second robot could reuse. The goal is easy adoption for any body: one arm, two arms, two arms and two legs, and unusual bodies such as a quadruped that grips with a jaw on its head.

## Decision

1. **Body model** (`uzumaki.body`). A body is parts (named joint groups from the model file, mounted in a tree), effectors (points that touch or hold, on a part), sensors, and capabilities (`locomote`, `reach`, `grasp`, `see`, and the implicit `stop`), each provided by a set of parts. Limbs are never classified: "arm" and "leg" are labels for people, not types the core reads.
2. **Components and configurations** (`uzumaki.robots`). Familiar components (`Arm`, `Gripper`, `Hand`, `Legs`, `WheeledBase`, `Torso`, `Neck`, `Head`, `Camera`) mount on each other and compile to the body model. Configurations (`SingleArm`, `TwoArms`, `MobileManipulator`, `Humanoid`, `Quadruped`, `QuadrupedWithArm`, `QuadrupedWithJaw`) are ready-made robots with named handles.
3. **Part claims.** Every tool call claims the parts it moves; calls run together only on disjoint parts, and `stop` claims every part and preempts.
4. **Task packs** (`uzumaki.tasks`). A pack names the capabilities it needs on one effector, the capability behind each tool, the planner's task card and the generator's capability specs. It binds to any body that satisfies it. `pick-place` is the first pack; its protocol `pick-place-v1` is unchanged.
5. **Embodiments and adapters** (`uzumaki.embodiments`). An embodiment pairs a robot description with an adapter module, loaded only on demand, that provides the reachable case set, fresh environments, the model file's names and the run identity.
6. **Conformance** (`python -m uzumaki.conformance`). A robot is supported on a task when it passes eight checks: body, model names, task binding, skill catalog, stop preemption, deterministic reset, replay and evaluator outcome.
7. **The harness stays at the decision level.** Joint control, gaits and balance belong to adapters.

## Consequences

- The SO-101 is a `SingleArm` whose joint names come from its description; its adapter is unchanged otherwise. The fixture arm is a `SingleArm` too. Both pass conformance.
- The planner's system prompt is composed from the task pack's task card. For `pick-place` it is byte-identical to `planner-v2`, so no prompt version changes.
- Not done yet, and planned for later chunks:
  - The episode driver and the evaluator still implement `pick-place-v1` rules; a second task pack will need its own runner.
  - Observations still follow contracts v1. Generic observation facts (contracts v2) come with cameras.
  - Claims are checked by the library but not yet enforced in the driver, which runs one call at a time; enforcement comes with the first two-arm task.
  - The generator's system prompt still says "tabletop pick-and-place".
