# 0003: Generated skills are sandboxed Python programs against the broker tools

Status: **candidate_for_review**. Roadmap feature 12 asks for one MVP skill representation chosen by a decision record. This record makes that choice. It does not change `pick-place-v1`, contracts 1.0, the registry format or the restricted worker.

## Options considered

- **Python program `run(tools, payload)`.** The restricted worker (feature 06) already runs this shape, and the broker already exposes exactly the four `pick-place-v1` tools. Models write Python well, and a failure gives a concrete trace: which call, with which arguments, got which reply.
- **Graph or DSL of skill nodes.** This is easier to check statically and to search over (GaP-style). It needs a new interpreter, a schema and a validator, and it is a second representation to maintain before the first one has produced evidence.

## Decision

- A generated skill is Python source that defines `run(tools, payload)`. It acts only through `tools.call("<tool>", {...})` with the tools its capability permits, receives `{"observation": ..., "parameters": ...}` and returns `{"outputs": ..., "failure": None | <diagnostic>}`.
- The harness, not the model, defines the skill around the program in a `CapabilitySpec`: ID, input and output contracts, applicability, tool permissions, resource bounds, completion and failure semantics. Each version's instructions carry the program's SHA-256, so the registry digest binds the code to the artifact.
- Static checks run before registration: size, syntax, a top-level `run(tools, payload)`, no imports except `math`, no dunder or private attributes, no reflective or I/O builtins, no classes or `with`, no code at import time, and literal permitted tool names. A program that fails them is never registered and never reaches the worker. These checks catch mistakes and make feedback precise. They are not isolation; the kernel boundary of the restricted worker is.
- The program runs only in `RestrictedWorker`. Its tool calls are answered by broker handlers that are the driver's own bounded, recorded tool calls, so budgets, admission, evidence and replay are exactly those of hand-authored skills.

## Consequences

- Graph representations stay a later experiment (roadmap 24), not an MVP prerequisite.
- A generated program can only run on a host that passes worker admission (Linux x86_64, Landlock ABI 6 or later, seccomp). Elsewhere generation fails before the first model call. GitHub-hosted runners may report a Landlock ABI below 6; there CI runs the static and session tests and skips worker execution, so CI alone does not evidence generated-program runs.
