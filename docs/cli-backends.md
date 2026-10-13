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
are queued and sent as the next turn with `resume_args`. Cancel and stop remove the container. Erasing a session runs the parser's `erase_args` (`opencode session delete
<id>`) in a throwaway, network-less container on the session's state volume.

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
`low_memory` or `gpu_held`. While a turn runs it holds the guard's lease, so a new hold drains it first, and one
agent turn on the inference gate, so an image job waits for it rather than stopping llama-server under it (endpoint
requests wait for it too). A turn cut off because llama-server stopped anyway fails with no retry. Without the local-model module the server must answer
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

## Tool profiles for hosted backends (#638)

A session's `tool_profile` is `default` (the tools each backend has always had) or `bash-only`, which leaves the model
a shell and no file read, write, edit, search or listing tools. Set it on the Web new-task form (Tools, shown only for
a backend that supports it) or as `tool_profile` on `POST /api/v1/sessions` and the Web session endpoints. It is
frozen on the session, kept by forks, and shown on the session info page. `GET /backends` lists what each backend
accepts as `tool_profiles`.

It follows arXiv 2609.20804 Finding 5 (§3.2, Tables 3 and 4): bash-only helped only the strongest model it tested
(Nemotron-3 550B, 65.8% to 69.4% on SWE-Bench at 53% lower cost) and one benchmark pairing, and hurt the rest
(Mistral-Medium-3.5-128B fell from 68.6% to 45.4% on SWE-Bench as file-localisation failures rose from 16.0% to
41.4%; the weakest model lost 15.0 points). **It is for strong hosted models only.** It is never offered for native
local sessions or `kind: cli` backends, which run the local model: the paper's weak-model regime, where predefined
tools won by 15.0 points on SWE-Bench and 10.1 on Terminal-Bench. The paper's §6 says its crossover points must be
validated before they transfer, so run the comparison below before relying on it. See
`docs/harness-design-gap-report.md` §4.4.

| Backend | `bash-only` | How |
| --- | --- | --- |
| Claude Code, `tool_mode: builtin` | yes | `--tools Bash` and nothing else |
| Claude Code, `tool_mode: split` | yes | the harness MCP server serves `run_shell` alone of the split tools |
| Codex, `tool_mode: split` | yes | the same: `run_shell` alone over MCP |
| Codex, `tool_mode: builtin` | no | its tools are controlled through `environments: []` and feature overrides, which remove the shell together with `apply_patch`; there is no shell-without-editor setting in the pinned CLI |
| Cursor | no | it takes no tool flag and is outside MCP |
| `kind: cli` (OpenCode) | no | runs the local model; its permission config could deny edit and read, but that is unchecked against the pinned 1.18 keys and the local model is the case the paper says loses |
| `local` | no | native local sessions keep their tools |

An unsupported backend refuses `bash-only` with `tool_profile_unsupported` (HTTP 400) rather than ignoring it, as
does a chat or App-tools-only session. If a backend's config changes while a session waits (Codex taken out of split
mode, say), the run fails with the same code instead of starting with the full tools.

What the model loses:

- **Claude Code builtin**: Read, Write, Edit, Glob, Grep, NotebookEdit, WebFetch, WebSearch, Task and TodoWrite. It
  reads, searches and edits files with shell commands (`cat`, `grep`, `sed`, heredocs). The harness's own MCP tools
  (web, session search, memory library, App tools) stay, and Bash calls go through the same approval path and smart
  reviewer as before.
- **Split mode (Claude Code and Codex)**: `read_file`, `write_file`, `edit_file`, `search` and `list_files`. Shell
  calls still run on the session sandbox with the same policy, grants, approvals and output bound. The CLI's own
  non-environment tools stay as in default split mode (Claude Code's `Task` and `TodoWrite`, Codex's plan tool), and so
  do the harness's other MCP tools. Split mode's `files_touched` list no longer sees edits, since they now happen in
  shell commands.

The harness's default profile has neither the paper's read-before-write check nor its post-edit diagnostics, so it
sits between the paper's two conditions; expect a smaller gap than the paper's either way.

Usage rows carry the session's profile. `GET /backends` adds `usage_by_profile` (the week's requests, tokens and cost
per profile) beside `usage_by_source`, and `harness_backend_cost_usd_total` has a `tool_profile` label.

### Running the comparison

The owner runs this; CI never makes hosted calls.

1. Pick a small task set, half shell-centric (the work is mostly commands: log digging, scripts, builds) and half
   edit-centric (changing a known set of files: a failing test, a rename). Three of each is enough to see a large
   effect; repeat each task to see a small one. Write down the files each edit task must change.
2. Run every task twice on the same backend, model and `tool_mode`, once per profile: the Web form's Tools choice, or
   `tool_profile` in the session request. Use a throwaway project or a git project's own task branches.
3. Grade each run yourself (did it do the task?) and write `tasks.json`:

   ```json
   {"runs": [
     {"session_id": "a1b2c3d4e5", "task": "fix-flaky-test", "task_type": "edit",
      "expected_files": ["tests/test_cache.py"], "success": true},
     {"session_id": "f6a7b8c9d0", "task": "fix-flaky-test", "task_type": "edit",
      "expected_files": ["tests/test_cache.py"], "success": false}
   ]}
   ```

4. Run `python -m bakeoff.tool_profile_compare --db D:/Agents/harness/harness.sqlite3 --tasks tasks.json --workspaces`
   on the server. `--db` is the main store, which holds the usage log; each session is read from Web's or its App's store beside it. Every database opens read-only, and `--json` prints the summary as JSON. `--workspaces` adds each
   workspace's diff to the files a run changed, so keep the workspaces until you have run it, or give each entry its
   own `files_changed`.

It prints tokens, cost and success per task type and profile, never pooling shell and edit tasks, and counts
localisation failures: edit runs that changed none of their expected files, the failure mode that grew under bash-only
in the paper. On the synthetic rows in `bakeoff/tool_profile_fixture.json`
(`python -m bakeoff.tool_profile_compare --runs bakeoff/tool_profile_fixture.json`):

```text
task type  profile    runs     success  prompt tok  completion tok  cost $   $/run  $/success  loc. fail
---------  ---------  ----  ----------  ----------  --------------  ------  ------  ---------  ---------
shell      default       2   1/2 (50%)     100,000           6,000  0.3901  0.1951     0.3901          -
shell      bash-only     2  2/2 (100%)      54,600           4,100  0.2253  0.1127     0.1127          -
edit       default       2  2/2 (100%)     216,700          12,600  0.8391  0.4195     0.4195        0/2
edit       bash-only     2   1/2 (50%)     132,300           7,400  0.5079  0.2540     0.5079        1/2
all        default       4   3/4 (75%)     316,700          18,600  1.2292  0.3073     0.4097        0/2
all        bash-only     4   3/4 (75%)     186,900          11,500  0.7332  0.1833     0.2444        1/2
```

These numbers are made up to show the format. Read the per-type rows, not `all`: a cheaper profile that loses edit
tasks is not a saving.
