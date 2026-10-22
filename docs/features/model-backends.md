# Model backends: any model for the planner and the generator

Status: implemented and tested offline with injected transports (`uzumaki.backends`, `uzumaki.gateway`, `uzumaki.anthropic_api`, `uzumaki.codex`). None of these backends has yet answered a live run in this repository.

The harness depends only on the provider interface: `request(dict) -> dict`, and optionally `request_within(request, timeout_ms)`. Requests and results are provider-neutral (see `uzumaki.gateway`), and every call runs inside `BoundedRun`, whatever answers it. Which model answers is configuration.

## Choosing a backend

Every command that calls a model (`python -m uzumaki.sim --planner model`, `uzumaki.sim.generate`, `uzumaki.sim.demo`) takes the same options. Each option can also be set from an environment variable:

| Option | Environment | Meaning |
|---|---|---|
| `--provider NAME` | `UZUMAKI_PROVIDER` | a backend from the table below (default `vercel`) |
| `--model ID` | `UZUMAKI_MODEL` | the model ID, in the backend's own naming |
| `--base-url URL` | `UZUMAKI_BASE_URL` | another endpoint for the backend |
| `--api-key-env VAR` | `UZUMAKI_API_KEY_ENV` | the *name* of the variable holding the key |
| `--record FILE` / `--replay FILE` | | keep exchanges, or answer from them with no model |

| Backend | Adapter | Key | Default model |
|---|---|---|---|
| `vercel` (alias `gateway`) | OpenAI-compatible | `AI_GATEWAY_API_KEY` | `anthropic/claude-sonnet-5.5` |
| `anthropic` | Messages API | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `openai` | OpenAI-compatible, reasoning dialect | `OPENAI_API_KEY` | none; `--model` needed |
| `openrouter` | OpenAI-compatible | `OPENROUTER_API_KEY` | none |
| `ollama` | OpenAI-compatible at `http://localhost:11434/v1` | none | none |
| `openai-compatible` | OpenAI-compatible at `--base-url` | optional, `--api-key-env` | none |
| `codex` | Codex CLI | its own sign-in (`codex login`) | the CLI's default |

`openai-compatible` covers vLLM, LM Studio, llama.cpp, LiteLLM, Groq, Together, DeepSeek and any other server that speaks chat completions. Plain http is accepted only for localhost; everything else must use https.

The make targets take the same choice:

```
make demo-live PROVIDER=anthropic
make demo-live PROVIDER=openai MODEL=gpt-5
make demo-live PROVIDER=ollama MODEL=qwen3
make demo-live PROVIDER=openai-compatible BASE_URL=https://host/v1 MODEL=m API_KEY_ENV=MY_KEY
make demo-live PROVIDER=codex
```

`live-smoke` and `generate-live` work the same way. `*-codex` targets remain as shorthands.

## Adapters

- **OpenAI-compatible (`GatewayProvider`)**: `POST {base}/chat/completions`, with the system prompt as the first message, `temperature` 0 and `max_tokens`. With `reasoning=True` (used for the `openai` backend), it sends `max_completion_tokens` instead, at least 4096, and no temperature, since OpenAI's reasoning models reject both.
- **Anthropic (`AnthropicProvider`)**: `POST {base}/v1/messages` with `x-api-key` and `anthropic-version: 2023-06-01`. It sends no sampling parameters, which current models reject. It asks for at least 4096 output tokens, because adaptive thinking counts toward `max_tokens`. Text blocks are joined and thinking blocks are skipped. A `refusal` stop reason is treated as invalid output. 429, 529 and 5xx responses are transient.
- **Codex (`CodexProvider`)**: runs `codex exec` with a ChatGPT sign-in; see the [model planner note](model-planner.md).

All adapters classify failures the same way: transient errors are retried, timeouts raise `ProviderTimeout`, and authentication and other client errors fail the run with a fixed code. Keys are read from the environment when a request is sent. They are never taken from flags, and never written to results, errors, evidence or exchanges.

## Adding a backend

A new endpoint for an existing protocol is one `Backend` entry in `uzumaki.backends.BACKENDS`. A new protocol needs an adapter that implements `request`/`request_within` with the same classification, plus a new `kind`.

## Limitations

- Prompts were written and tested against a scripted model. Different models may need different numbers of repair rounds, or may fail the output contract more often; the run records this rather than hiding it.
- Default model IDs are unverified until a live run confirms that the backend serves them under those names.
- Tool calling, streaming and prompt caching are not used; every call is one plain request and one response.
