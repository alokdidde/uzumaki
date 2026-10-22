# Generated skills with trace-guided repair in the SO-101 simulation (features 12, 13 and 16, first cut)

Status: implemented and tested offline with a scripted model in place of Claude (`uzumaki.generator`, `uzumaki.sim.generate`). No live model has generated a program yet; `make generate-live` is that run once the gateway is reachable. The representation is chosen in [decision 0003](../decisions/0003-skill-program-representation.md).

## What it does

The harness asks the generator role for a missing skill, `transfer-box`: move the box to the target and release it, written from the tool contracts alone, with no hand-authored body to copy.

1. **Generate.** One model call per round (prompt `generator-v1`, role `generator`, through the same `GatewayProvider` and `BoundedRun` budgets as the planner). The context is a JSON data block with the capability card: goal, input and output schemas, permitted tools, `stop_on` failures and the worker's tool-call limit. The answer must contain one fenced `python` block; anything else is invalid output and is retried within budget.
2. **Check.** `check_program` rejects programs that are oversized or have invalid syntax, programs without `run(tools, payload)`, imports other than `math`, dunder or private attributes, reflective or I/O builtins, classes, `with` blocks, code at import time, and tool names that are not literal or not permitted. A rejected program is never registered and never runs. Its reasons are the next round's feedback.
3. **Register and run.** A program that passes becomes the next `candidate` version of `transfer-box`. The skill's instructions carry the program's SHA-256. The program runs as a development trial on every development case through the unchanged driver, trial selector and evaluator. It runs in `RestrictedWorker`, and its `tools.call` requests are answered by broker handlers that make the driver's own bounded, recorded tool calls. If the harness stops during one of those calls (budget, cancellation, crash), the program gets `harness_stopped` replies with no effect, and the stop is raised in the host once the worker has exited. A program that crashes, exceeds its limits or returns a malformed value fails the case with a fixed detail.
4. **Judge.** A case passes when the evaluator's primary outcome and run status match the frozen expectation with zero violations. Where the case expects completion, the evaluator's candidate verdict must also accept the program, so report-only or motionless success is rejected.
5. **Repair.** If any development case fails, the version is marked `rejected`. The next request carries the parent program and feedback for up to six failed cases: expected and observed outcome, the failed stage (`program`, `output_contract`, `perception`, `planning`, `execution` or `verification`, taken from the first failing call), the worker status and error type, and the program's own tool calls with their replies. Lineage is kept (`parent`). A program with the same syntax tree as an earlier one stops the session as `repeated_candidate`. Running out of rounds or budget also stops it.
6. **Confirm.** An accepted program is run frozen, with no feedback and no further model calls, on cases the generator never saw.

The generator never sees evaluator code, the world's privileged state, other case sets or the confirmation cases. Nothing it writes can change the cases, expectations, budgets, worker limits or acceptance rule, because those are host values. Promotion to `validated` is not done here; that is Step 4 (feature 17).

## On the SO-101

`python -m uzumaki.sim.generate` uses the 16 development cases (the eight fault mirrors plus `so101-dev-009` to `016`) for repair and the 12 cases `so101-dev-017` to `028` for confirmation. These confirmation cases are development cases withheld from repair. They are not a held-out set: SO-101 held-out seeds must still come from the protocol custodian.

With the scripted model in `tests/generated_programs.py`, the session runs as follows:

| Round | Program | Development cases | Feedback that drives the next round |
| --- | --- | --- | --- |
| 1 | places with the observation it picked with | 3/16 | `place` rejected `stale_observation` (stage `perception`) |
| 2 | observes again before placing, but always picks first | 15/16 | on `stale-after-pick` the retry while holding gets `pick` rejected `already_held` (stage `planning`) |
| 3 | skips the pick when already holding; maps rejections to diagnostics | 16/16, accepted | none |

Then the frozen round-3 program passes 12/12 confirmation cases. The whole run takes about 40 s on this host. The scripted programs are hand-written stand-ins for model output. They show that the loop, the feedback and the judgement work, not how well Claude repairs.

## Running it

```
make generate-live   # live: Claude through the gateway, video of the accepted program
python -m uzumaki.sim.generate --replay build/generate-exchanges.json --out build/generate-replay   # offline
```

Output: `round-<n>.py`, `round-<n>/<case>.json` bundles, `confirmation/` bundles, `summary.json` (lineage, per-case outcomes, failed stages, model calls and tokens) and, with `--video`, `accepted-<case>.mp4`. Do not commit the exchanges file or bundles.

## Tests

- `tests/test_generator.py` (always runs, no worker):
  - answer parsing and every static-check reason;
  - rejected programs never reach the worker or the registry;
  - repair requests carry the static-check reasons;
  - a repeated program stops the session;
  - invalid and rate-limited answers are retried;
  - budget exhaustion ends the session, and session arguments are validated;
  - the artifact binds the program digest.
- `tests/test_generator_worker.py` (admitted platforms; part of `make containment`):
  - two repairs from trace feedback reach acceptance on the eight fixture cases, with the exact feedback checked and no evaluator or privileged fields in any request;
  - accepted programs replay from their bundles;
  - success claims without action are rejected;
  - crashes, malformed outputs and tool floods fail the case with program feedback;
  - a world crash inside a program's call fails the run exactly as for hand-authored skills.
- `tests/test_sim.py::test_generated_skill_is_repaired_and_confirmed_on_the_so101` (simulator and admitted platform): the CLI end to end on the SO-101, including the confirmation run and checks that confirmation case IDs never reach the generator.

## Limitations

- No live generation yet. How many rounds Claude needs, and at what cost, is unmeasured.
- One capability (`transfer-box`) and one representation. The planner does not yet hand a missing capability to the generator; that hand-off is part of Step 4.
- Feedback is per round from the parent only, with no memory of older rounds or reusable repair guidance (feature 19).
- Development acceptance is all-or-nothing on 16 cases, with no uncertainty estimate (feature 14).
