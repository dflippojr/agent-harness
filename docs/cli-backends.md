# Config-declared CLI backends (#168)

A headless print-mode agent CLI can be a session backend through a `backends:` entry with `kind: cli` and a
parser registered in `harness/cli_backends.py` (`PRINT_MODE_PARSERS`), with no session class of its own. The first one
is OpenCode on the local llama-server. Claude Code, Codex and Cursor stay built-in backends (`kind` empty).

## Config

```yaml
backends:
  opencode:
    enabled: false
    kind: cli
    image: agent-harness-cli:1
    model: qwen3.6-35b-a3b            # one of models:
    command: [opencode, run, --format, json, --auto, -m, "llama/{model}"]
    resume_args: [--session, "{session_id}"]
    parser: opencode_jsonl
    network: harness-cli-opencode
    volume: harness-state-opencode
    max_sessions: 1
    permission_mode: auto             # display only
    # card_note: ...                  # Settings → Backends text; defaults to the text below
```

- `command` and `resume_args` are argv lists. `{model}` and `{session_id}` are filled in per element; nothing goes
  through a shell. The prompt is always the last element, after `--` (writing `-- "{prompt}"` at the end is allowed
  and means the same). Any other placeholder, or `--` anywhere else, is a config error.
- v1 is keyless and local only. `proxy`, `api_key_file` and `oauth_token_file` are rejected: a provider proxy or key
  needs its own issue and a recorded provider-policy review first. `billing` is `local`, `auth` is `none`, `mcp` is
  off, and there is no login.
- `model` must be one of the harness `models:`. Sessions on the backend pick a model from that list too.

## A turn

Each turn is one `docker run --rm` of `command` (as Cursor's print mode is). The container gets:

- only the backend's internal network, with no proxy variables, no key and no route out;
- the workspace bind-mounted at `/workspace`, and nothing else from the host except the generated CLI config;
- the CLI state volume (one per domain: `harness-state-opencode` for Web, `harness-cli-opencode-app-<id>` for an App),
  holding `XDG_DATA_HOME` and `XDG_STATE_HOME`;
- the sandbox memory, CPU and pids limits, `no-new-privileges`, dropped capabilities and the session label.

The first turn's events carry the CLI's session id, stored as `run.backend_session_id`. Messages that arrive mid-turn
are queued and sent as the next turn with `resume_args`. Cancel and stop remove the container.

OpenCode auto-approves (`--auto`) inside that container; branch review is the outside check. Its config is generated
per session from the harness `models:` entry and mounted read-only at `/etc/opencode/opencode.json`, OpenCode's
managed config, which outranks the workspace's `opencode.json` and `.opencode/`. It keeps `autoupdate: false`,
`share: disabled` and denies `webfetch`, `websearch` and `question` (a headless question would hang the turn).

`opencode_jsonl` maps `opencode run --format json` (1.18.30) records: `text` parts become the answer (the last message
with text), `tool_use` becomes a tool call and its result (an error when `state.status` is `error`), `step_finish`
counts a step, and `error` fails the session. A process exit with no `error` ends the turn; a non-zero exit fails it.
Lines that aren't JSON objects are skipped. Usage is not reported.

## The route to llama-server

`docker compose -f ops/cli-llm/compose.yaml up -d` (the Windows installer runs it when the local model is installed)
creates the internal `harness-cli-opencode` network and its only other member, `harness-cli-opencode-llm`, a socat
forwarder that passes exactly one port (`LLAMA_PORT`, default 8090) to the host. The forwarder reaches the host
through its own bridge, which no CLI container joins. It is never on `harness-egress`. While the network or the
forwarder is missing, the backend shows as unavailable and sessions on it fail with `backend_unavailable`. Windows
host only; the MacBook runner is out of scope.

## The local model

`max_sessions: 1`, and llama-server's own queue shares the slot with native sessions and warmup. Before every turn
the runner asks the local-model module (`ModuleRuntime.local_turn_admission`) to admit it: the turn waits in `queued`
while the GPU guard holds the GPU (the session gets `gpu_paused` and `gpu_resumed`), waits for RAM like a parked
model's load, then loads a parked model with `ensure_loaded`. A model that can't load fails the session with
`low_memory` or `gpu_held`. While a turn runs it holds the guard's lease, so a new hold drains it first; a turn cut
off because llama-server stopped fails with no retry. Without the local-model module the server must answer
`/health`, or the session fails saying so.

## Apps

A `kind: cli` backend is invisible to every App until the owner grants it with
`POST /api/admin/v1/provider-credentials` (`policy: subscription`, no `secret_ref`), whether or not the App's
providers are otherwise managed. Until then the App's settings refuse it (`provider_policy`), discovery shows it as
unavailable and sessions get `provider_not_allowed`. An App's end user and a household member can't use it (no
per-person login, no member key).

Settings → Backends shows: "Runs without asking for approval inside its sandbox. Uses the local model, which is much
weaker than the hosted backends. Usage is not reported." and "No login needed (local model)".

## Adding another CLI

Add a parser class to `PRINT_MODE_PARSERS` with `parse(line) -> list[event]` and an `answer` property, plus
`config_target`, `config(model, base_url)` and `env` if the CLI takes a generated config. Record fixtures from the
real CLI for its tests (`tests/fixtures/`). Gemini CLI is #214.
