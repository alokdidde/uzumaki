# Contributing

The repository and Project are private. Keep credentials, private observations, execution bundles, datasets, and weights out of Git. No open-source license has been selected.

Work in ascending Sequence order in [the roadmap](docs/roadmap.md). The dependency list is authoritative: a later item becomes Ready only after required predecessors pass. Optional experiments are Deferred and do not block MVP. Documentation-only decisions need documentary checks; executable tests start with feature 03.

Before coding, freeze the feature workload, environment, criteria, correctness and containment guards, metrics/thresholds, reproduction command, and evidence destination. Use a `codex/` branch and a small reviewable PR for implementation work. Keep model/provider/simulator choices scoped and versioned. Tests must exercise meaningful behavior and failure paths, not mirror implementation details.

Generated artifacts stay candidates until independent evaluation accepts them. Do not change the evaluator, protected holdout, or acceptance thresholds to obtain a pass. Do not execute generated code in the parent process or treat structured outputs as proof of correctness. New cloud calls, model training, and physical hardware tests must be explicit, budgeted experiments.

Include the issue, resulting behavior, exact validation commands, retained evidence, and limitations in the PR. Request independent review for substantive model, evaluation, sandbox, or architecture changes. Mark an issue complete and update Project readiness only after its acceptance evidence passes. Keep issue text and canonical documentation consistent; document any change to scope or order rather than silently skipping dependencies.

## Local checks

Install once: `python -m pip install -r requirements-dev.txt && python -m pip install --no-deps -e .`. Then run `make check` offline before proposing changes. Never commit secrets, datasets or raw execution bundles.
