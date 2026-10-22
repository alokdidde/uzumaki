# One-command demo: missing skill to promoted skill on the SO-101 (features 17 and 18, slim)

Status: implemented and tested offline with a scripted model for both roles (`uzumaki.promotion`, `uzumaki.sim.demo`). No live model has run the whole demo yet. `make demo-live PROVIDER=...` is that run, with any [model backend](model-backends.md).

## The loop

`python -m uzumaki.sim.demo` runs the MVP loop from the README diagram on one SO-101 task (default `so101-dev-020`, a withheld case):

1. **Before.** The model planner gets the task with a library that has no skill able to move the box: the hand-authored set without `pick-box`, `place-box` and `pick-and-place`. It should abstain, and the evaluator records `abstained`.
2. **Missing capability.** An abstention with no completion starts the generator for `transfer-box`. Repair on the 16 development cases follows [skill generation](skill-generation.md).
3. **Confirmation.** The accepted program runs frozen on the 12 withheld cases, including the task case. There is no feedback and no further model call.
4. **Promotion.** `promote` validates the skill only if every development case and every confirmation case passed, the confirmation cases do not overlap the development cases, and every bundle names this exact skill version as its candidate. The promotion record keeps:
   - the skill and program SHA-256;
   - the prompt version and model;
   - the full candidate lineage;
   - the SHA-256 of every development and confirmation bundle.
   The registry transition carries the record's `evidence_ref`.
5. **Library.** The library is saved to `<out>/library`:
   - `skills.json`: every skill with its state and evidence reference;
   - `programs/<id>@<v>.py`;
   - `promotions.json`: promotion and rollback records, append-only.
6. **After.** The library is loaded back from disk. Each program is checked against the digest in its skill and in its promotion record. The same planner then gets the same task again, can choose `transfer-box`, and the evaluator decides the outcome. The command exits 0 only if this run completed with zero violations.

`uzumaki.promotion.rollback(library, "transfer-box@3", reason=...)` deprecates a promoted version. It stays in the library for provenance, is no longer eligible and gets no body, and a rollback record is appended.

With the scripted model (`tests/generated_programs.py::DemoModel`), the run goes like this:

- The planner abstains and the outcome is `abstained`.
- Generation takes three rounds: 3/16, then 15/16, then 16/16 development cases.
- The frozen program passes all 12 confirmation cases, and `transfer-box@3` is promoted.
- The planner selects it, and the task case completes with zero violations.

The whole run takes about 45 s on this host. Recorded exchanges replay the same run offline.

## Running it

```
make demo-live                       # live, Vercel AI Gateway (AI_GATEWAY_API_KEY)
make demo-live PROVIDER=anthropic    # live, Claude direct (ANTHROPIC_API_KEY)
make demo-live PROVIDER=codex        # live, Codex CLI signed in with ChatGPT
make demo-live PROVIDER=ollama MODEL=qwen3   # live, a local model
python -m uzumaki.sim.demo --replay build/demo-exchanges.json --out build/demo-replay   # offline
```

Output in `build/demo`:

- `before/`, `round-<n>.py` and `round-<n>/`, `confirmation/` and `after/` bundles;
- `library/`;
- `summary.json`;
- `after.mp4` (needs `MUJOCO_GL=osmesa` or `egl`, and `ffmpeg`).

Do not commit the exchanges file, bundles or library.

## Tests

- `tests/test_promotion.py` (admitted platforms; part of `make containment`) covers:
  - a promotion record with full provenance;
  - refusal without confirmation, with overlapping or failed confirmation, with evidence from another skill, without an accepted program, and twice for the same version;
  - a library round trip that keeps every state;
  - tampered, mismatched or missing programs;
  - rollback.
- `tests/test_sim.py::test_one_command_demo_closes_the_loop_on_the_so101` runs the demo CLI end to end on the SO-101. It also checks that the task case never reaches the generator.

## Limitations

- These are development results. The confirmation cases are development cases withheld from repair, not a held-out set, so promotion here is not acceptance under `pick-place-v1`. Held-out seeds must come from the protocol custodian.
- One task family, one generated capability, one simulated arm. No physical-robot claim.
- Promotion is all-or-nothing on 28 cases, with no uncertainty estimate or comparison runner (feature 14).
- The library is local files. There is no signing and no concurrent writers.
