# Claude planner through the Vercel AI Gateway (features 09 and 10, first cut)

Status: implemented and tested offline (`uzumaki.gateway`, `uzumaki.planner`). It has run live through the Codex CLI backend: on 2026-10-22 `live-smoke` completed the nominal case with the box within 1.3 mm of the target, and the planner both abstained and then completed the task in the [one-command demo](end-to-end-demo.md#live-runs). It has not yet run against the live gateway, because this cloud environment's network policy blocks `ai-gateway.vercel.sh` and no key is configured in it. `make live-smoke` is the first gateway run once both are in place. Beyond these single runs, no claim about live model behaviour is made here.

## What it does

- **Provider:** any backend in [model backends](model-backends.md); the planner does not depend on which. `uzumaki.gateway.GatewayProvider` is the adapter for an OpenAI-compatible chat-completions gateway. The default is the Vercel AI Gateway at `https://ai-gateway.vercel.sh/v1`, with model `anthropic/claude-sonnet-5.5`; that model name is unverified until the live smoke run. It implements the existing provider interface, so every call runs inside `BoundedRun` and counts against the run's model-call, retry, token and output budgets. HTTP 429, 5xx and connection failures are transient and retried. Timeouts are `ProviderTimeout`. Authentication and other client errors fail the run with a fixed code. A response of the wrong shape becomes invalid output. Usage is recorded only when both token counts are reported; otherwise it stays unknown. The key is read from `AI_GATEWAY_API_KEY` at send time and never appears in results, errors or evidence. `RecordingProvider` keeps exchanges in the `RecordedProvider` format, so a live run replays offline.
- **Planner (`uzumaki.planner.ModelPlanner`):** a `DecisionSource` for the unchanged baseline driver. Each decision sends a pinned system prompt (`planner-v2`; v2 tells the model that listed skills already passed their applicability checks) and one JSON data block: the task, the fresh observation (ID, gripper state, target pose) and the currently eligible skills that have an implementation, with each skill's instructions, applicability and input schema from the pinned snapshot. The model must answer with exactly one JSON object, either `select` (eligible ID and version, parameters) or `abstain`. Anything else is invalid output, retried within budget. The driver then validates the choice again (eligible set, pinned hash, input contract) before any tool call. The planner cannot run code or call tools, and only the evaluator decides completion.
- **Evidence:** decisions record the planner role's model version (`<model>+planner-v2`), the input digest and the output, including the model's reported usage and latency. Recorded-decision replay works as for the rule selector. Tool call IDs no longer depend on how many model calls were made: `BoundedRun` now numbers model and tool calls separately.
- **Budgets:** `MODEL_BUDGETS` has the protocol's 20 model calls per case and 2 retries, with the same 3 attempts, 40 steps and 120 s case clock as the baseline.

## Running it

```
python -m uzumaki.sim --planner model --case nominal --out build/live --record build/live-exchanges.json   # live
python -m uzumaki.sim --planner model --replay build/live-exchanges.json --case nominal --out build/replay  # offline
```

## Tests

- `tests/test_gateway.py`, with an injected transport and no network, covers:
  - the request and result contract and the key header;
  - usage that is unknown, malformed or has extra fields;
  - rate limits, 5xx, disconnects and timeouts;
  - authentication and client errors;
  - a missing key;
  - provider schema changes and malformed requests;
  - an https-only base URL;
  - live exchanges recorded and replayed offline without the key.
- `tests/test_planner.py` drives the offline fixture loop with a fake model and covers:
  - **Frozen expectations:** every frozen fixture case meets its expectations and replays.
  - **Contents of the model request:** the context holds only declared data, with no evaluator, case or held-out fields.
  - **Answers that are retried:** prose, invented skills, wrong versions, unknown actions and extra fields are retried. If invalid answers persist, the budget runs out before any actuation.
  - **Answers that end the run:** parameters outside the contract stop it as `invalid_decision` before actuation. Abstention stops without actuation, and a claim of success is not completion.
  - **Rate limits** are retried.
- `tests/test_sim.py` also runs the SO-101 `nominal`, `failed-grasp` and `unreachable-goal` cases with the model planner, and checks that the recorded exchanges replay to an identical bundle.

## Limitations

- No live run through the gateway yet, and only single runs through Codex. Model behaviour, latency and cost through the gateway are unmeasured, and so is the comparison with `rule-selector-v1` on the same cases.
- The case clock is simulated time, so model latency does not count against the 120 s case budget yet. Latency is recorded with each decision.
- Context is the current decision only: no history of earlier attempts or retrieved guidance (the rest of feature 11).
