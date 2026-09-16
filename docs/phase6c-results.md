# Phase 6c: inference endpoint

Built 2026-09-15. Decisions (user): clients are coding tools on the MacBook, the user's own scripts, and Anthropic-API
clients; endpoint requests go ahead of the next agent turn.

## Design

llama-server b10950 already speaks both APIs: `/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
`/v1/messages` and `/v1/messages/count_tokens` (checked against the binary and live). `/v1/embeddings` is also
supported through a separately configured llama-server started with `--embedding`; llama.cpp restricts that mode to
dedicated embedding models, so the route is not advertised against the normal Qwen generation server. Instead of a
separate gateway
(LiteLLM), the endpoint is a thin proxy inside the daemon (`harness/endpoint.py`). A separate process couldn't share
the GPU with agent turns, the GPU guard, or the session queue.

| Concern | How |
| --- | --- |
| Where | The daemon's existing `tailscale serve` HTTPS: `https://<tower>.<tailnet>.ts.net/v1` (OpenAI) and the bare origin (Anthropic). Local scripts can use `http://127.0.0.1:8100`. A tailnet login is still required from other devices (daemon middleware). |
| Auth | Per-device keys `hk-...` sent as `Authorization: Bearer` or `x-api-key`; only the sha256 is stored (`api_keys` table). Created and revoked in Settings → Inference endpoint, or with `POST /keys` and `DELETE /keys/{id}`. |
| Models | Unknown generation names ("gpt-4o", "claude-sonnet-5") map through `endpoint.model_aliases` (fnmatch), falling back to the default model. A configured embedding route always rewrites the requested name to its dedicated embedding model. `GET /v1/models` returns both generation and configured embedding models. |
| GPU sharing | `InferenceGate` (`harness/scheduler.py`) sits under the session queue, per model call. An endpoint request waits only for the agent call already in flight; agent calls wait while endpoint requests are queued or running. Once an agent call has waited `agent_fair_seconds` (90), new endpoint requests line up behind it. More than `max_waiting` (4) waiting requests get 429. |
| GPU guard | While a game or Plex transcode has the model unloaded: 503 with `Retry-After: 180` (`count_tokens` still works; it doesn't use the GPU). The guard waits for endpoint requests in flight before stopping the server, like agent turns. |
| Streaming | Server-sent events relayed chunk by chunk; the gate slot is held until the stream ends or the client disconnects (which also stops generation upstream). |
| Accounting | `endpoint_requests` table: key, route, model, stream, status, tokens, GPU wait and total time. Tokens come from `usage`, or from llama-server's `timings` when a streaming client didn't ask for usage. Metrics `harness_endpoint_*`; dashboard row "Inference endpoint". |
| Discovery | `GET /v1/capabilities`: routes, models with context size, features (streaming, tool calls, reasoning, optional embeddings and images), aliases, guard state. The embedding feature and route appear only when both `endpoint.embedding_base_url` and `endpoint.embedding_model` are set. |

## Verification

Tests: 80 pass (`tests/test_phase6.py` adds auth, key revoke, model mapping, both response shapes, SSE relay,
accounting, metrics, guard 503, and gate ordering, including the fairness rule).

Live on the tower (2026-09-15):

| Check | Result |
| --- | --- |
| `GET /v1/models`, non-streaming chat (`model: gpt-4o` → Qwen) | answered "391" for 17×23 in 0.7 s |
| **Claude Code 2.1.272** with `ANTHROPIC_BASE_URL` set to the tailnet URL, `ANTHROPIC_AUTH_TOKEN` set to a harness key, and a throwaway `CLAUDE_CONFIG_DIR` | `claude -p "Read shop.py and tell me the tax rate"` used the Read tool and answered "7%" in 25 s (2 `/v1/messages` calls) |
| OpenAI streaming tool call (`tools: get_weather`) | `get_weather({"city":"Lisbon"})` streamed in 4.2 s |
| Queue jump: endpoint request while session `92f0f8ca53` was mid-run | waited 203 ms for the agent's call in flight, answered in 0.6 s; the session then finished normally (5 turns) |
| Streamed usage without `include_usage` | recorded from `timings` after the fix |
| MacBook over Tailscale (issue #6 follow-up) | disposable standard-library probe fetched models, received 128 SSE chunks and a requested `get_weather` tool call in 20.94 s; temporary key revoked afterward |

Not tested live: the 90 s fairness rule and 429 under real load (both covered by tests).

### Embeddings follow-up (2026-09-15, issue #10)

Added the OpenAI-compatible `/v1/embeddings` proxy, model/capability discovery and accounting. The current Qwen server
was deliberately not reused: the installed llama.cpp binary documents `--embedding` as a dedicated-model-only mode,
and a live request against the generation server did not complete. Configure a second local server, for example on
`127.0.0.1:8091`, with an embedding model and set:

```yaml
endpoint:
  embedding_base_url: http://127.0.0.1:8091
  embedding_model: nomic-embed-text-v1.5
  embedding_context_tokens: 8192
```

Until both required values are present, capability discovery reports `embeddings: false`, omits the route/model and a
direct call returns a descriptive 404. Tests cover authentication, disabled discovery, model rewriting, request
normalization, proxy response and endpoint accounting. A real embedding model download/server deployment remains a
separate operational choice rather than silently adding another always-loaded model to the 16 GB GPU.

## Client setup notes

- OpenAI-compatible tools (opencode, Continue, Zed, scripts): base URL `https://<tower>/v1`, API key = a harness key,
  any model name.
- Claude Code / Anthropic SDK: `ANTHROPIC_BASE_URL=https://<tower>`, `ANTHROPIC_AUTH_TOKEN=<key>`,
  `ANTHROPIC_MODEL=qwen3.6-35b-a3b`, and `CLAUDE_CODE_MAX_CONTEXT_TOKENS=65536`. Without the last one Claude Code
  warns that it doesn't know the model and assumes a 200K window, far beyond Qwen's 64K. Expect agentic tools with
  long system prompts to be slow on first use (Qwen reads ~800 tok/s; later turns reuse the cache).
- Every request from a tool goes ahead of agent turns, so a chatty editor (autocomplete on every keystroke) slows
  running tasks. Give such a tool its own key, so it shows up separately on the dashboard.
