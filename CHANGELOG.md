# Changelog

Documentary changes to canonical contracts and protocols. Entries describe specifications, not runtime capability.

## Unreleased

### Added — a Claude-subscription backend, a record of every model call, finer provider errors

Ported from the issue-09 draft (#35) onto the current backend layer.

- `uzumaki.claude_acp.ClaudeAcpProvider` and the `claude` backend (`--provider claude`): Claude Code with its Claude sign-in over the Agent Client Protocol (`claude-code-acp`, or `UZUMAKI_ACP_COMMAND`). One fresh agent process and session per request; an environment allowlist that never passes API-billing settings; permission, file and terminal requests refused; killed at the deadline.
- `uzumaki.provenance.ProvenanceLog`: every model command records one contract v1.0 `model_provenance` record per call, failed and retried calls included, with a fixed error code, in `<out>/model-provenance.json`. Digests, IDs, numbers and codes only.
- `uzumaki.faults.RateLimited` and `ProviderDisconnected` (both `TransientProviderError`, so retried as before). The OpenAI-compatible, Anthropic and Codex adapters raise them for 429/529 or usage limits and for connections closed mid-reply.
- Tests: `tests/test_claude_acp.py` against a scripted local agent (`tests/fake_acp_agent.py`), `tests/test_provenance.py` (records validated against the contract schema), new cases in the adapter tests.

Not claimed: a live run of the `claude` backend.

### Added — The Go1 walks: a learned locomotion policy on the `fetch` pack

- `uzumaki.sim.go1_walk` and the `go1_walk` embodiment (`QuadrupedWithJaw`, task pack `fetch`): the Go1 with its neck and jaw on MuJoCo Playground's Go1 model, its legs driven by Playground's Go1 joystick policy (ONNX, run with `onnxruntime`). Both are pinned from MuJoCo Playground at `12ad7f38` (Apache-2.0); the meshes come from the pinned Menagerie Go1. `go_to` walks to the work station. Case set `go1-walk-dev-v1` (28 cases, TidyBot's construction on seeds `go1-walk-1001` to `1028`).
- `uzumaki.sim.assets.PinnedModel` gains `repository` and `commit`, so a pinned model can come from a repository other than Menagerie; `go1_walk` is the first. The simulator identity names the source.
- `requirements-sim.txt` adds `onnxruntime` (with `flatbuffers` and `protobuf`).
- No-op hooks for robots on soft legs: `SimWorld._replan_grasp` and `TidybotWorld._replan_place`. TidyBot's world reads its rig, reach and station from the instance, so the walking Go1 reuses it. `uzumaki.sim.go1.add_neck_and_jaw` builds the neck and jaw for either Go1.
- `python -m uzumaki.sim --robot go1_walk`, `make sim-demo-go1-walk`, `tests/test_go1_walk.py`, [feature note](docs/features/go1-walk-fetch.md), GIFs in `docs/media/`.

Not claimed: held-out results, walking while manipulating, navigation beyond a walk to a table, a policy trained for this neck and jaw, a physical Go1.

### Added — Unitree Go1 with a jaw: a quadruped that grips with its head

- `uzumaki.sim.go1` and the `go1` embodiment (`QuadrupedWithJaw`): the Unitree Go1 from MuJoCo Menagerie at `cda5236f` (BSD-3-Clause) standing on its legs with a free body, with a six-joint neck built on its trunk and the UMI gripper (MIT, also pinned) mounted on it as the jaw. It runs the frozen `pick-place-v1` geometry unchanged at a 0.2 m table (case set `go1-dev-v1`). 28/28 meet their expectations, conformance passes, and 200 extra geometries all complete.
- `python -m uzumaki.sim --robot go1`, `make sim-demo-go1`, `tests/test_go1.py`, [feature note](docs/features/go1-simulation.md), GIFs in `docs/media/`.

Not claimed: held-out results, walking, a real Go1 neck or jaw (both are modelled here), a physical robot.

### Added — Unitree G1: a humanoid at the table

- `uzumaki.sim.g1` and the `g1` embodiment (`Humanoid`): the Unitree G1 with Dex3 hands from MuJoCo Menagerie at `cda5236f` (SHA-256 pinned with its BSD-3-Clause license), its pelvis on a stand. The waist and left arm place the hand; the seven finger joints follow one grip value, the middle finger is coupled to the index finger, and flat pads on the fingertips and thumb grip the box. It runs the frozen `pick-place-v1` geometry unchanged (case set `g1-dev-v1`). 28/28 meet their expectations, conformance passes, and 187 of 200 extra geometries complete (the other 13 are refused as out of the left hand's reach, with no contact).
- `python -m uzumaki.sim --robot g1`, `make sim-demo-g1`, `tests/test_g1.py`, [feature note](docs/features/g1-simulation.md), GIFs in `docs/media/`.

Not claimed: held-out results, standing free or walking, the right hand, Unitree's hand controller, a physical G1.

### Added — Spot with arm: a legged robot standing at the table

- `uzumaki.sim.spot` and the `spot` embodiment (`QuadrupedWithArm`): Spot with its arm from MuJoCo Menagerie at `cda5236f` (SHA-256 pinned with its BSD-3-Clause license). It stands on its legs with a free body, and planning follows the stance the physics holds. It runs the frozen `pick-place-v1` geometry unchanged (case set `spot-dev-v1`). 28/28 meet their expectations, conformance passes, and 200 extra geometries all complete.
- `uzumaki.sim.arm.ArmRig` gains `customize` (scene edits before compiling) and `world` (a `SimWorld` subclass per robot).
- `python -m uzumaki.sim --robot spot`, `make sim-demo-spot`, `tests/test_spot.py`, [feature note](docs/features/spot-simulation.md), GIFs in `docs/media/`.

Not claimed: held-out results, walking, Boston Dynamics' gripper force or controller, a physical Spot.

### Added — TidyBot and the `fetch` task pack: a body that moves

- `fetch-v1` and the `fetch` task pack (`uzumaki.tasks.FETCH`): needs `reach` and `grasp` and also `locomote`; tools `observe`, `go_to` (base or legs), `pick`, `place`, `abort`; observation fields `base_pose` and `within_reach`; case fault `stopped_short_drives`. Budgets, outcomes, evaluator contract and evidence are `pick-place-v1`'s, with `go_to` counted as actuation.
- Skills `go-to`, `pick-box`, `place-box` (`tests/fixtures/skills_fetch_v1.json`), `fetch-selector-v1`, a planner task card, and a scripted offline world with two tables (`uzumaki.fetch_env`).
- `uzumaki.sim.tidybot` and the `tidybot` embodiment (`MobileManipulator`): the Stanford TidyBot from MuJoCo Menagerie at `cda5236f` (SHA-256 pinned with its license and the Kinova Gen3 and Robotiq 2F-85 licenses) on the shared arm engine. Case set `tidybot-dev-v1`: eight fault cases including `stopped-short`, twenty nominal cases from a seed recipe. 28/28 meet their expectations, conformance passes, 197 of 200 extra geometries complete as expected.
- `uzumaki.sim.arm`: unlimited joints get a ±π range for inverse kinematics.
- `python -m uzumaki.sim --robot tidybot`, `make sim-demo-tidybot`, `tests/test_tidybot.py`, `tests/test_fetch.py`, [feature note](docs/features/tidybot-fetch.md), GIFs in `docs/media/`.

Not claimed: held-out results, navigation beyond a straight drive to a table, part claims enforced at run time, the model planner or skill generation on `fetch`, a physical TidyBot.

### Added — ALOHA and the `handover` task pack: two arms on one task

- `handover-v1` and the `handover` task pack (`uzumaki.tasks.HANDOVER`): roles `giver` and `taker` bound to two grasping effectors; tools `observe`, `pick`, `handover`, `place`, `abort` with declared part claims; observation field `holder`; case fault `dropped_handovers`. Budgets, outcomes, evaluator contract and evidence are `pick-place-v1`'s.
- Skills `pick-box`, `hand-over`, `place-box` (`tests/fixtures/skills_handover_v1.json`), `handover-selector-v1`, a planner task card, and a scripted offline world (`uzumaki.handover_env`). The driver, broker and fixture loader take the protocol as a parameter.
- `uzumaki.sim.aloha` and the `aloha` embodiment (`TwoArms`): ALOHA from MuJoCo Menagerie at `cda5236f` (SHA-256 pinned with its BSD-3-Clause license) as two rigs on the shared arm engine. Case set `aloha-dev-v1`: eight fault cases including `dropped-handover`, twenty nominal cases from a seed recipe. 28/28 meet their expectations, conformance passes, 100 extra geometries all complete.
- `python -m uzumaki.sim --robot aloha`, `make sim-demo-aloha`, `tests/test_aloha.py`, `tests/test_handover.py`, [feature note](docs/features/aloha-handover.md), GIFs in `docs/media/`.

Not claimed: held-out results, part claims enforced at run time, the model planner or skill generation on `handover`, a physical ALOHA.

### Added — Franka Panda: a second arm on a shared simulation engine

- `uzumaki.sim.arm`: the SO-101's kinematics, planning, scene and world code as one engine driven by an `ArmRig` (robot description, pinned model, base placement, actuators, gripper, tool-centre site, pads, IK seeds). All 28 SO-101 cases give byte-identical bundles after the move.
- `uzumaki.sim.panda` and the `panda` embodiment: the Franka Emika Panda from MuJoCo Menagerie at `cda5236f` (SHA-256 pinned with its license). It runs the frozen `pick-place-v1` geometry unchanged: case set `panda-dev-v1` is the eight frozen fault cases plus nominal cases on `dev-001` to `dev-020`. 28/28 meet their expectations, conformance passes, and 200 extra geometries all complete.
- `uzumaki.sim.assets.PinnedModel`: one pinned Menagerie directory per robot (`so101`, `panda`, `aloha`); `python -m uzumaki.sim.assets fetch so101 panda aloha`.
- `python -m uzumaki.sim --robot panda`, `make sim-demo-panda`, `tests/test_panda.py`, [feature note](docs/features/panda-simulation.md), GIFs in `docs/media/`.

Not claimed: held-out results, a physical Panda, the real robot's torque interface.

### Added — robot-neutral interfaces: body model, components, task packs, conformance

- `uzumaki.body`: any robot as parts, effectors, sensors and capabilities (`locomote`, `reach`, `grasp`, `see`, implicit `stop`), validated, with a canonical document and SHA-256; `PartClaims` admits concurrent calls only on disjoint parts and lets `stop` preempt.
- `uzumaki.robots`: components (`Arm`, `Gripper`, `Hand`, `Legs`, `WheeledBase`, `Torso`, `Neck`, `Head`, `Camera`) and configurations (`SingleArm`, `TwoArms`, `MobileManipulator`, `Humanoid`, `Quadruped`, `QuadrupedWithArm`, `QuadrupedWithJaw`) that compile to the body model.
- `uzumaki.tasks`: task packs that bind to any body with the capabilities they need; `pick-place` is the first. `uzumaki.planner.TaskCard`: the planner prompt comes from the pack (byte-identical for `pick-place`).
- `uzumaki.embodiments`: `fixture-arm` and `so101` as `SingleArm` embodiments with on-demand adapters (`uzumaki.sim.adapter`).
- `python -m uzumaki.conformance --embodiment NAME --task NAME`: eight checks; both embodiments pass.
- [Decision 0004](docs/decisions/0004-embodiments.md), [Add your robot](docs/guides/add-your-robot.md). Tests: `test_body.py`, `test_robots.py`, `test_tasks.py`, `test_conformance.py`, and SO-101 conformance in `test_sim.py`.

Not claimed: any robot other than the SO-101 and the fixture arm running a task; claims enforced in the driver; generic observations (contracts v2).

### Added — model backends: any model for the planner and the generator

- `uzumaki.backends`: a registry of backends selected with `--provider`/`--model`/`--base-url`/`--api-key-env` (or `UZUMAKI_PROVIDER`, `UZUMAKI_MODEL`, `UZUMAKI_BASE_URL`, `UZUMAKI_API_KEY_ENV`): `vercel` (alias `gateway`), `anthropic`, `openai`, `openrouter`, `ollama`, `openai-compatible` and `codex`. All three model CLIs use it.
- `uzumaki.anthropic_api.AnthropicProvider`: the Anthropic Messages API, raw HTTP, with the same request, result and error contract.
- `uzumaki.gateway.GatewayProvider`: any OpenAI-compatible base URL, keyless local servers (plain http only to localhost), and a reasoning dialect (`max_completion_tokens`, no temperature) for OpenAI's API.
- Make targets take `PROVIDER`, `MODEL`, `BASE_URL` and `API_KEY_ENV`; the `*-codex` targets are shorthands for `PROVIDER=codex`.
- Tests: `tests/test_backends.py`, `tests/test_anthropic_api.py`, new cases in `tests/test_gateway.py`; the demo test now runs through the `anthropic` backend. [Feature note](docs/features/model-backends.md).

Not claimed: a live run on any of these backends.

### Added — promotion with provenance and the one-command demo (features [17](https://github.com/alokdidde/uzumaki/issues/17) and [18](https://github.com/alokdidde/uzumaki/issues/18), slim)

- `uzumaki.promotion`: `promote` (validates an accepted generated skill only with passing, non-overlapping confirmation evidence for that exact version; the record keeps digests, prompt, model, lineage and every bundle's SHA-256), `save_library`, `load_library` (checks each program against its skill's and record's digest) and `rollback` (deprecates, with an appended record).
- `python -m uzumaki.sim.demo`: before-run with the planner abstaining, then generation, confirmation, promotion into `<out>/library`, and the after-run from the reloaded library. `make demo-live`, `make demo-live-codex`. `uzumaki.sim.generate` gains shared `--provider`/`--model`/`--record`/`--replay` helpers.
- Tests: `tests/test_promotion.py` (in `make containment`), the demo end to end in `tests/test_sim.py`, and `DemoModel` in `tests/generated_programs.py`. [Feature note](docs/features/end-to-end-demo.md).

Not claimed: a live run of the demo, held-out acceptance or physical-robot behaviour.

### Added — generated skills with trace-guided repair in simulation (features [12](https://github.com/alokdidde/uzumaki/issues/12), [13](https://github.com/alokdidde/uzumaki/issues/13) and [16](https://github.com/alokdidde/uzumaki/issues/16), first cut)

- [Decision 0003](docs/decisions/0003-skill-program-representation.md): a generated skill is a Python program `run(tools, payload)` against the broker tools, wrapped in a harness-defined `CapabilitySpec`.
- `uzumaki.generator`: the generator role (prompt `generator-v1`), `check_program` static checks before registration, `SandboxedProgram` (a skill body that runs the program in `RestrictedWorker` with broker handlers that are the driver's recorded tool calls), `evaluate_program`, and `generate_skill`: a bounded session of generate, check, register, evaluate on development cases and repair from trace feedback, with lineage, repeated-program detection and `rejected` marking. The capability is `TRANSFER_BOX`.
- `uzumaki.baseline.SkillContext.fail`: a skill body can fail the run with a fixed detail.
- `python -m uzumaki.sim.generate [--record|--replay] [--video]`: 16 SO-101 development cases for repair, 12 withheld cases for a frozen confirmation run. `make generate-live`; `make containment` now also runs the worker tests of the generator.
- Tests: `tests/test_generator.py`, `tests/test_generator_worker.py`, `tests/generated_programs.py` (scripted model) and an end-to-end generation test in `tests/test_sim.py`. [Feature note](docs/features/skill-generation.md).

Not claimed: live generation by Claude (only a scripted model has been used), held-out acceptance, or promotion (feature 17).

### Added — Codex CLI provider (ChatGPT sign-in) and planner prompt v2

- `uzumaki.codex`: `CodexProvider` runs each planner request as one read-only, ephemeral `codex exec` turn signed in with a ChatGPT account; same request/result contract and error classes as `GatewayProvider`, usage from `turn.completed`. `python -m uzumaki.sim --provider codex`, `make live-smoke-codex`. Tests: `tests/test_codex.py`.
- `uzumaki.planner`: prompt `planner-v2` says listed skills already passed their applicability checks. Under `planner-v1` the first live run abstained because the context omits the box pose the model was asked to judge.

### Added — Claude planner through the Vercel AI Gateway (features [09](https://github.com/alokdidde/uzumaki/issues/9) and [10](https://github.com/alokdidde/uzumaki/issues/10), first cut)

- `uzumaki.gateway`: `GatewayProvider` (OpenAI-compatible chat completions, default Vercel AI Gateway, key from `AI_GATEWAY_API_KEY`, fixed-code errors, transient/timeout/rejected classification, usage only when reported) and `RecordingProvider` (live exchanges as `RecordedProvider` files).
- `uzumaki.planner`: `ModelPlanner` (prompt `planner-v1`, bounded data-only context from the pinned snapshot, strict JSON select/abstain answers validated before the driver validates again).
- `uzumaki.baseline`: `MODEL_BUDGETS`; `run_case`, `plan_case` and `replay_case` take a planner ID, budgets and a provider; `correctness_failures(..., model_calls=None)`. `uzumaki.supervisor`: model and tool calls are numbered separately, so tool call IDs replay without model calls.
- `python -m uzumaki.sim --planner model [--record|--replay]`, `make live-smoke`. Tests: `tests/test_gateway.py`, `tests/test_planner.py`, model-planner cases in `tests/test_sim.py`. [Feature note](docs/features/model-planner.md).

Not claimed: any live model behaviour. The gateway host is blocked in the cloud environment used here, so nothing has called the live gateway yet.

### Added — MuJoCo SO-101 simulation (early slice of [feature 15](https://github.com/alokdidde/uzumaki/issues/15))

- `uzumaki.sim` (optional, `requirements-sim.txt`): the pinned Menagerie SO-101 model, fetched and SHA-256 verified (`uzumaki.sim.assets`); the `so101-dev-v1` case set (`uzumaki.sim.cases`, `src/uzumaki/sim/data/so101_cases_v1.json`); the scene; top-down inverse kinematics and collision-checked grasp and place planning (`uzumaki.sim.so101`); `SimWorld` and `SimEvaluator`, which add physics, forbidden-contact detection and a reset check to the fixture contracts; and a runner and CLI (`python -m uzumaki.sim`) with optional MP4 video.
- `uzumaki.baseline`: `run_case` with an injectable `CaseEnvironment`; `run_fixture_case` now uses it, with behaviour unchanged.
- Tests: `tests/test_sim.py`. The case-set and asset-pin checks always run. The simulator tests skip without the simulator, and fail instead under `make sim`. CI gains a `sim` job. Makefile: `sim-assets`, `sim`, `sim-demo`. Demo GIFs are in `docs/media/`.
- [Decision 0002](docs/decisions/0002-so101-simulation.md) records why the SO-101 needs its own case set; [feature note](docs/features/so101-simulation.md).

Not changed: `pick-place-v1` rules, contracts 1.0, the fixture baseline and its cases, and every other module. Not claimed: held-out acceptance, physical-robot behaviour, or any model-driven behaviour.

### Added — offline task-execution baseline ([issue 08](https://github.com/alokdidde/uzumaki/issues/8))

- `uzumaki.fixture_env`: the frozen case format `uzumaki.baseline-cases/1.0` (`load_cases`), a deterministic fixture clock and `FixtureWorld`, which holds privileged state, decides admission for the four `pick-place-v1` tools behind the broker and logs every actuation. It also adds `FixtureEvaluator`, the only outcome authority (protocol completion, primary-outcome precedence, violation counting, `candidate_verdict`), and `FixtureBackend`, which injects worker crashes and goal withdrawal. Kinematics are scripted, there is no contact model, and timings are fixture constants.
- `uzumaki.baseline`: `RuleSelector` (`rule-selector-v1`) over the current eligible set, hand-authored bodies for `pick-box@1`, `place-box@1` and `pick-and-place@1` limited to their permitted tools, `plan_case` (pins a snapshot; optional development-trial candidate), `run_fixture_case` (observation, eligibility, decision validation, bounded execution and retries in `BoundedRun`, settling checks, independent verdict and final bundle), `replay_case` (recorded-decision replay), `correctness_failures` (`CORRECTNESS_CHECKS`) and `export_evidence`.
- Fixtures: `tests/fixtures/baseline_cases_v1.json` (eight cases with expected outcomes) and `tests/fixtures/candidates_pick_place_v1.json` (adversarial `hold-still` and `report-success` candidates).
- Tests: `tests/test_baseline.py`, run by `make check`. Every frozen case passes every correctness check and replays to the same semantic outcome as a fresh run. Further tests cover divergent, version-changed and tampered replays, invalid selector decisions, eligibility, tool permissions, and rejection of motionless and report-only candidates with a positive control. `make baseline-evidence` retains the raw bundles and a hash manifest in `build/evidence/baseline/`. Feature note: [docs/features/offline-baseline.md](docs/features/offline-baseline.md).
- Review repair (findings r1-F1, r1-F2, r1-F3): the pipeline and fixtures were not executed by any test, the adversarial candidates had no bodies so their rejection was unreachable, and the documentation still described no harness loop or evaluator. Added the test module above, hand-authored trial bodies and a positive control in the tests, the reproduction and evidence target, and updated architecture, testing, README and this entry.
- Validation repair (finding followup1-validation-2): `test_nominal_bundle_records_the_whole_loop` expected 11 recorded observations, confusing the 11 world steps with observations. The nominal run makes 9 observes (2 in the attempt, 7 while settling) and records one observation per observe; pick and place produce none. The test now requires the observation count to equal the number of world `observe` calls, which is 9. Source behavior is unchanged, and the feature note now gives the correct count.

Not changed: the provider API, the registry, broker, sandbox, lifecycle, budget and evidence modules, `make check`, CI, the protocol and contracts 1.0. Not claimed: simulator, physical, held-out or live-model behavior; skill bodies and the selector are trusted host code and do not run in the restricted worker. Not added: a model-based planner or generator, a simulator adapter or promotion.

### Added — versioned skill registry ([issue 07](https://github.com/alokdidde/uzumaki/issues/7))

- `uzumaki.registry`: immutable `uzumaki.skill/1.0` artifacts (`load_skill`, `seal_skill`, `skill_digest`) with ID, integer version, SHA-256 over canonical JSON, instructions, closed input/output contracts (`check_parameters`, `check_result`), applicability, versioned tool permissions, resource bounds (`budgets()` → `Budgets`), completion semantics (success authority is always the environment evaluator) and failure semantics (`retry_on`, `stop_on`, unlisted failures return to the coordinator), plus dependency version ranges. Rejections raise `SkillError` with a fixed `reason` from `REASONS`.
- `SkillRegistry`: thread-safe, versioned (`registry_version`, `history()`), with separate `candidate`, `validated`, `deprecated` and `rejected` states. Only `candidate→validated`, `candidate→rejected` and `validated→deprecated` are allowed. It rejects tampered, duplicate and conflicting artifacts, non-increasing versions, tools outside the declared catalog or at another version, and missing, incompatible (out of range, rejected, unvalidated, wider tool permissions) or cyclic dependencies. `snapshot()` is immutable. `pin()` returns a `PinnedSkill` with its own verified artifact and resolved validated dependency closure, which later registry changes cannot alter. `RegistrySnapshot.eligible_skills(...)` emits the contract 1.0 `eligible_skills` record.
- `load_skill_set` loads a hand-authored `uzumaki.skillset/1.0` file offline. The fixture is `tests/fixtures/skills_pick_place_v1.json` (unsealed bodies, sealed by the tests).
- Tests: `tests/test_registry.py`. Feature note: [docs/features/skill-registry.md](docs/features/skill-registry.md).
- Review repair (finding r1-F1): dependency closures resolved each edge on its own, so one pin could hold two versions of one skill ID, and disjoint diamond ranges were still pinned and eligible. Resolution is now a bounded backtracking search (`MAX_RESOLUTION_STEPS`) for one version per skill ID that satisfies every range on that ID. Pinning, `eligible` and `validated` transitions use validated versions only. Registration uses non-rejected versions and rejects an unsatisfiable closure as `incompatible_dependency`. New tests cover overlapping diamonds (one shared version), disjoint diamonds at registration, at validation and at pinning after a deprecation, and an unchanged existing pin.
- Validation repair (finding followup1-validation-2): `RegistrySnapshot.registry_version` was a plain attribute, so a caller could reassign a snapshot's version. `RegistrySnapshot` now uses `__slots__`, exposes `registry_version` as a read-only property, and raises `AttributeError` on any attribute assignment or deletion. New test: `test_snapshot_and_registry_versions_cannot_be_reassigned`.

Not changed: the provider API, the broker, sandbox, lifecycle, budget and evidence modules, `make check`, CI, the protocol and contracts 1.0. Not claimed: signatures, verified promotion evidence, rollback or persistence (feature 17 and later). Not added: a harness loop, selector, planner, evaluator, simulator adapter or live model.

### Added — restricted worker for generated candidates ([issue 06](https://github.com/alokdidde/uzumaki/issues/6))

- `uzumaki.sandbox`: `RestrictedWorker(broker, limits, protected_paths=...)` runs candidate source (`run(tools, payload)`) in a fresh `python -I -S -B` child in its own session, with a scrubbed environment and only the broker pipes inherited. `uzumaki._sandbox_boot` applies rlimits (CPU, address space, file size, open files, no core), `no_new_privs`, a Landlock domain (read-only stdlib, system libraries and candidate directory; one writable scratch directory; TCP, signals and abstract UNIX sockets scoped) and an x86_64 seccomp filter (no sockets, fork, exec, namespaces, ptrace, rlimit changes, mounts, BPF, io_uring or keyrings; threads only), then verifies them before loading the candidate. Failures are `containment_failed` and the candidate never runs.
- Admission: `probe_platform()` and `admit()` accept only Linux x86_64 with Landlock ABI ≥ 6 and seccomp filters; anything else raises `ContainmentUnavailable` before a process starts. Protected paths overlapping anything the worker can reach are refused (`protected_path_exposed`).
- Host-side authority: wall-time watchdog, output, message and tool-call limits, process-group kill, reaping and a `/proc` survivor sweep (`tree_clean`). `WorkerLimits` and `WorkerReport` (statuses `returned`, `candidate_error`, `timeout`, `cpu_limit`, `output_limit`, `protocol_violation`, `tool_budget_exhausted`, `crashed`, `containment_failed`; `sandbox_version` `uzumaki.sandbox/1.0`).
- `uzumaki.broker`: `ToolBroker` (protocol `uzumaki.broker/1.0`) with a versioned manifest of `ToolSpec`s whose handlers run in the host. Undeclared tools, version mismatches and malformed requests are rejected without calling a handler; arguments are recorded by digest only. `pick_place_v1_broker` accepts only the `pick-place-v1` tools.
- Tests: `tests/test_sandbox_admission.py`, `tests/test_broker.py` and the adversarial `tests/test_sandbox_containment.py` (file escape, credentials, network, subprocess escape, hangs, CPU, memory, output and file floods, broker abuse, crashes). `make containment` runs them and fails instead of skipping on a non-admitted host. The network test still runs on hosts whose test process cannot open sockets (inherited network guard); there it checks only that every candidate attempt is denied. Feature note: [docs/features/restricted-worker.md](docs/features/restricted-worker.md).
- Review repair (findings followup1-F1, followup1-F2): Landlock does not mediate metadata operations or SysV IPC, and the worker runs as the host UID. The seccomp filter now also denies, with `EPERM`, `chmod`/`chown`/`utime` and every variant (`fchmod*`, `*chownat`, `utimes`, `futimesat`, `utimensat`), xattr writes, removals and reads (including the `*xattrat` calls), `file_setattr`, `inotify_*`, `fanotify_*`, SysV IPC (`shm*`, `sem*`, `msg*`, `semtimedop`) and POSIX message queues (`mq_*`). The bootstrap self-check also requires `chmod("")` and `msgctl(-1, IPC_STAT)` to fail with `EPERM` (`metadata_not_restricted` and `ipc_not_restricted` otherwise). New tests: a decoded-filter check, an adversarial metadata test that requires mode, owner, mtime, ctime, xattrs and content of protected assets to stay unchanged, and an IPC test that requires no SysV object or queue to survive the run. `stat`/`access` visibility of guessable paths is documented as a remaining limitation.
- Review repair (finding r3-F3): the worker shares the host UID and PID namespace, and `setpriority`, `sched_setparam`, `sched_setscheduler`, `sched_setattr`, `sched_setaffinity` and `ioprio_set` need only a matching UID, so Landlock does not mediate them. A candidate could renice, deprioritise or pin the watchdog parent or every process of the user, persistently. The seccomp filter now denies all six with `EPERM` for every target, the worker included, and the bootstrap self-check requires `setpriority` with an invalid `which` to fail with `EPERM` (`scheduling_not_restricted` otherwise). New tests: the decoded-filter check covers the six numbers, and an adversarial test attempts each call (raw and through `os`) on a host victim, the test process that owns the watchdog, the worker itself and `PRIO_USER`/`PRIO_PGRP`/`IOPRIO_WHO_USER` targets. It requires `EPERM` and an unchanged nice value, policy, priority, affinity and I/O priority for the victim and the parent. Read-only scheduling queries and PID probing in the shared PID namespace are documented as remaining visibility.

Not changed: the provider API, `uzumaki.deadline` (still not a sandbox), the evidence format, `make check`, CI, the protocol and contracts 1.0. Not claimed: a tested platform (pending independent credential-free validation), cgroup or namespace isolation, total disk quotas, physical safety. Not added: a harness loop, skill registry, evaluator, simulator adapter or live model.

### Added — lifecycle, budgets, cancellation and failure handling ([issue 05](https://github.com/alokdidde/uzumaki/issues/5))

- `uzumaki.lifecycle`: a thread-safe contract 1.0 run lifecycle (same states, edges and event names). The first terminal request wins. Repeats record `terminal_repeat_ignored` and other disallowed requests record `lifecycle_violation`; neither raises nor changes state. It adds harness statuses `completed`, `abstained`, `blocked`, `partial_progress`, `timeout`, `budget_exhausted`, `cancelled`, `failed` and `rejected`, and monotonic progress (`none`, `partial`, `goal_reached`) that cancellation never reduces. Post-terminal observations are kept separately.
- `uzumaki.budgets`: strict `Budgets` for wall time, model calls (retries included), steps, retries, attempts, per-call and total output bytes, per-call timeouts, and optional tokens and cost. Usage counts only when the provider reports it. Unknown usage is never zero; `unknown_usage` is `fail_closed` (default) or `record_unknown`.
- `uzumaki.supervisor.BoundedRun`: enforces the budgets before and after each model or tool call, and retries timeouts, transient errors and malformed results within budget. Worker failures fail the run. It fences late results by run, attempt and call ID (recorded by digest, never applied) and handles cancellation at call boundaries. It makes no call after the run leaves `running`, releases registered resources exactly once, and provides `execute(logic)` and a `RunReport`.
- `uzumaki.deadline`: `run_in_process` and `ProcessProvider` run a call in a spawned child process group, kill it at the deadline or after the reply, and bound its output. This bounds calls and cleans up processes; it is not a sandbox.
- `uzumaki.faults`: `TransientProviderError`, `ProviderTimeout`, `WorkerFailure`.
- Tests: `tests/test_lifecycle.py`, `tests/test_budgets.py` and `tests/test_supervisor.py` (fake clock), and `tests/test_deadline.py` (real processes), with helpers `tests/fakes.py` and `tests/process_workers.py`. Feature note: [docs/features/lifecycle-budgets.md](docs/features/lifecycle-budgets.md).

- Review repair 1 (findings r0-F1, r0-F2): a voluntary stop at or after the wall deadline now ends the run as `timeout` (`wall_time`), and results arriving exactly at a call deadline are fenced. History entries record the contract `edge` separately from the actual trigger `on`, plus `by` and `evaluated: false`, so harness stops are never labelled as evaluator events. Progress keeps its source (the default is `harness_logic`), and `completed` needs `goal_reached` observed by `environment` or `evaluator`.

Not changed: the `RecordedProvider` API (still usable as a `BoundedRun` provider), the evidence log format `uzumaki.events/1.0`, `make check`, CI, the protocol and contracts 1.0. Not added: a sandboxed worker (feature 06), an evaluator, environment abort/safe-stop, retry backoff, persisted run reports, live models or simulators.

### Added — execution events and semantic replay ([issue 04](https://github.com/alokdidde/uzumaki/issues/4))

- `uzumaki.evidence`: `EventRecorder` writes a versioned (`uzumaki.events/1.0`, contract `1.0`) event log with run/task/candidate IDs, skill and model versions, observation identities (content digests, not raw content), decisions, tool calls and results, the outcome and timings. Events are SHA-256 chained from the manifest identity. `Limits` bound event count, event bytes and bundle bytes. The validator defaults are the ceiling, so the recorder rejects larger limits at construction. Bytes are counted exactly, and room for `run_finished` stays reserved, so exceeding a limit is an error when the offending event is recorded, never in `finish`, and is never silent dropping.
- Sensitive-key redaction at record time with declared `redacted_paths`. Redacting a replay input marks the run non-replayable with a reason.
- `validate_bundle`, `load_bundle` and `write_bundle` reject malformed, oversized, version-mismatched, truncated, reordered, corrupt, incomplete and redaction-inconsistent evidence with fixed `EvidenceError` reasons.
- `uzumaki.replay`: distinct `recorded_decision`, `fresh_model_reevaluation` and `environment_resimulation` modes. Only recorded-decision replay is implemented; it uses no provider or environment and compares semantic outcomes. The other modes raise `UnsupportedReplayMode`. `CLAIM_REQUIREMENTS` and `assess_claims` declare the artifacts each claim needs.
- Tests: `tests/test_evidence.py`, `tests/test_replay.py`, the toy harness `tests/toy_harness.py` and the fixture `tests/fixtures/events_provider.json`. Feature note: [docs/features/event-evidence.md](docs/features/event-evidence.md).

Not changed: the provider API, `make check`, CI, the protocol and contracts 1.0. Not added: a harness loop, the contract `execution_event`/`model_provenance` emitters, signatures, live models or simulators.

### Added — package bootstrap and offline CI ([issue 03](https://github.com/alokdidde/uzumaki/issues/3))

- Stdlib-only `src/uzumaki` package with `py.typed`, pinned Python 3.12.12 and a complete pinned `requirements-dev.txt`.
- `RecordedProvider` (recorded/fake, versioned JSON, no network) with safe `ValueError` failures, a deterministic fixture and unit tests.
- `make check` (ruff, format check, mypy, pytest, offline build) and a read-only-permission CI workflow with SHA-pinned actions. See [docs/features/package-bootstrap.md](docs/features/package-bootstrap.md). No runtime execution or live model support.

### Added — harness and model contracts 1.0 ([issue 02](https://github.com/alokdidde/uzumaki/issues/2))

- [docs/contracts/v1.json](docs/contracts/v1.json) adds contract version `1.0`:
  - eight closed, self-contained JSON Schema Draft 2020-12 record schemas;
  - the authority map: the planner may only select or abstain, the generator produces only untrusted artifacts, and the evaluator is the task-success authority;
  - limits of 65536 input bytes and a 200 ms observation age;
  - the validation order, admission checks, harness semantic rules and the run lifecycle (`queued`, `running`, `cancelling`, then terminal `completed`, `cancelled`, `failed`, `rejected`).
- [docs/contracts/fixtures.json](docs/contracts/fixtures.json) adds 22 valid and 42 invalid concrete fixtures covering malformed, missing, stale, contradictory, oversized and unsupported-version inputs, authority violations, unknown-versus-zero usage and evidence, late-result fencing, cancellation, eligible-set version skew, before-attempt actuation and outcome/lifecycle consistency.
- Review repair 1 (findings r0-F1 to r0-F4): the 200 ms boundary fixture now uses one observation ID throughout; admission adds `actuation_before_observe`, which ends the case as `missing_observation`; the objective carries the full `profile.completion` rule; and the outcome schema enforces lifecycle/primary/progress consistency.
- Review repair 2 (findings r1-r0-F2, r1-r1-F5): the `actuation_before_observe` admission check now runs first, on harness state and before record validation, and the outcome mapping keys the before-attempt rule on the attempt state, so a malformed pick before `observe` ends the case as `missing_observation` rather than `tool_rejected`. The outcome schema now requires at least 1 violation for `collision` or `invariant_violation` with complete invariant evidence. New fixtures cover both.
- [docs/contracts/README.md](docs/contracts/README.md) and [docs/features/harness-contracts.md](docs/features/harness-contracts.md) describe the specification, decisions and limitations.
- [README.md](README.md), [docs/architecture.md](docs/architecture.md) and [docs/testing.md](docs/testing.md) now link the contracts.

Not changed: the issue-01 `pick-place-v1` protocol and decision 0001. Not added: any package, validator code, CI, harness, worker, evaluator, simulator or model integration (feature 03 onward).
