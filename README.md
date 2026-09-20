# agent-harness

Personal agent harness for `dflippotower`: agents run on the basement PC against a local model
**or** your own Claude Code, Codex, or Cursor subscription, and are driven from the phone or
MacBook over Tailscale. The phased plan lives in the agent memory library
(`categories/project-ideas/capsules/local-agent-harness.md`).

Phases 0–8 are built. Open work is GitHub issues, mirrored from `docs/backlog.yaml`.

## Product names

| Name | Meaning |
| --- | --- |
| **Agent Harness** | The overall product, project, repository, and installed system |
| **Agent Harness Server** | The host service and its APIs; technical code and service-manager material may call its process the daemon |
| **Agent Harness Web** | The first-party browser UI and installable PWA in `harness/web` |
| **Agent Harness CLI** | The installed `harness` command |
| **Agent Harness Runner** | A remote execution worker, such as the **Mac Runner** |
| **Agent Harness SDK** | The supported client library in `sdk/` |
| **Agent Harness App** | A third-party integration that consumes the App API |
| **Agent Harness for Mac** | The Mac distribution that installs the CLI and Mac Runner together |

The `agent-harness-ci` pool (three self-hosted members on the tower) runs the full suite on pull requests and pushes
to `main`. Only after the `CI` workflow passes for that exact pushed commit does the `workflow_run` pipeline publish
sandbox images to GHCR and let the repository-scoped tower runner perform the fail-closed deployment. The
`agent-harness-review` pool (three self-hosted members) runs automated PR review; rebuild and add/remove steps for
both pools are in [`docs/CI-CD.md`](docs/CI-CD.md). See that page for the trust boundary and recovery details.

## Install

The default full profile runs on Windows 10/11 or x86-64 Linux and needs an NVIDIA GPU (12 GB+). Apple Silicon
macOS uses the hosted-provider service profile. Docker and Git are required on every platform.

```powershell
git clone https://github.com/dflippojr/agent-harness; cd agent-harness
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

For a hosted-provider-only Agent Harness Server with no local model or GPU requirement:

```powershell
powershell -ExecutionPolicy Bypass -File install\install.ps1 -Profile Service
ops\backends\login.ps1 codex # or claude / cursor
```

Linux/NVIDIA or Apple Silicon macOS:

```bash
install/install.sh                         # Linux defaults full; macOS defaults service
ops/backends/login.sh codex                # service profile: or claude / cursor
```

No admin rights needed. The guide is [`docs/INSTALL.md`](docs/INSTALL.md); the API for Agent Harness Apps is
[`docs/app-api.md`](docs/app-api.md) (Agent Harness SDK in `sdk/`); the owner API is
[`docs/admin-api.md`](docs/admin-api.md); the typed settings registry is [`docs/config-registry.md`](docs/config-registry.md);
profile details are in [`docs/service-profile.md`](docs/service-profile.md);
and bundled/separate Agent Harness Web deployment is in [`docs/web.md`](docs/web.md).

Machine owners can optionally isolate a hosted-provider API key and model allowlist per app. Key values remain in
owner-managed files, apps receive only sanitized policy and their own usage, and revocation never falls back to
another app's or the machine owner's key. See the per-app provider credentials section in `docs/admin-api.md`.

## Phase 8: subscription backends, Remote Control, answer checks

Built as mini-phases (`docs/phase8a-design.md`, `docs/phase8b-results.md`, `docs/phase8c-results.md`).
Terms and billing notes for app builders are in `docs/app-api.md`.

- Hosted backends (`backends:` in `config/harness.yaml`): unmodified `claude`, `codex`, and Cursor
  `agent` CLIs run in a provider-only Docker sandbox under your own login. Sign in once with
  `ops\backends\login.ps1 <backend>` (Windows) or `ops/backends/login.sh <backend>` (Unix). New task, templates and
  jobs pick a backend; Settings →
  Backends shows model/effort, 5h/7d usage and popular-model presets. CLI permission prompts become
  ordinary harness approvals (web, ntfy, or `/api/v1`). Optional user API-key fallback lives in
  `D:/Agents/harness/secrets/<backend>-api-key` and is never returned from an API. Cursor print mode
  uses `--force` only inside its workspace-mounted container, with branch review afterwards.
- Remote Control (`remote_control:`): Profile → Claude Remote Control starts
  `claude remote-control --spawn worktree` in a trusted tower git folder so you can pair from the
  Claude app. Those sessions are native Claude Code (no harness queue, sandbox or transcripts).
  Trust is Claude's own dialog; the harness never accepts it. Untrusted folders offer **Trust in
  Claude**, which opens a visible `claude` window on the tower. API:
  `GET/POST /remote-control/{project}` and `/api/v1/remote-control` (scope `remote_control`).
  Eligible entries are tower projects whose `repo` is a local folder plus any native-only paths in
  `remote_control.folders`; the latter never appear as sandboxed harness session projects.
- Quote checks (`web.quote_check`): a quoted passage of 25+ characters in a final answer must
  appear in something the agent read. One fix request, then a ⚠ flag on the answer and the Done
  notification. `web_fetch` reads GitHub repository and folder pages through the public API (license,
  README, file list) and file pages from `raw.githubusercontent.com`, falling back to HTML.

## Agent Harness Web

Agent Harness Server serves Agent Harness Web as an installable phone PWA (Agents, Jobs, Images,
Profile/Settings). After Phase 8 it grew into the first-party operator UI rather than a thin session list:

- Agents / Jobs / Images are tabs; session Transcript / Changes / Info are tabs. Session titles sit
  on the page, stay sticky, and are editable (`PATCH`/`PUT /sessions/{id}`). Jump arrows appear when
  you are about 0.75 viewport from an end. The session list filters by machine and skips a rebuild
  while a finger is down so the first tap opens a card.
- Review shows merge-conflict filenames and **Ask agent to resolve**. Scratch/mac-scratch is
  explained as a disposable empty folder, not a git repo.
- Settings: Memory (editable agent profile through the approved-write path), Appearance (themes,
  home-screen icon, in-app text size), Backends as controls including local Qwen, Disk used/free
  bars and cleanup. New task / New job / job details / Backends paint from `GET /backends?auth=skip`
  (~50 ms); live login/usage fills in afterwards.
- Mac sessions get `generate_image`: after ComfyUI finishes on the tower the PNG is copied into the
  Mac workspace (`put_file`, runner 4.1). Redeploy the runner (`ops\macbook\deploy.ps1`) to pick
  that up.
- Time-boxed guest/demo access (`guests:` in untracked `config/harness.local.yaml`): a named tailnet
  login can browse read-only until an ISO `until`. Requires `allowed_logins`. Guests cannot start or
  cancel work, approve, mint keys, pause the GPU, use Remote Control, edit memory, or Review.
- Household members (Settings → Accounts): the owner provisions a Tailscale login with a disk quota and
  session caps. Members authenticate only with that exact `Tailscale-User-Login`, see only their own
  projects/sessions, and run local-model tower tasks. Owner Home/search/review never show member prompts
  or diffs. Open-owner mode (empty `allowed_logins`) remains only while no members exist. The machine
  owner can still read local storage; this isolation is API/UI, not a hostile-administrator boundary.
  See `docs/admin-api.md` and `docs/app-api.md`.
- Agent Harness Web is a first-party Server browser client ([`docs/web.md`](docs/web.md)). Bundled static serving
  remains the default, while the same no-build PWA can be hosted separately with a configurable Server URL and an origin-bound
  `ho-` owner token. Session work dogfoods `/api/v1`; owner operations dogfood `/api/admin/v1`. Authenticated SSE,
  images, and downloads work without putting bearer tokens in URLs. App tokens cannot call the owner surface.
  Members never construct `/api/admin/v1` requests.

Still open from that pass: **#48** (add agent-harness and the memory library to Remote Control) and
**#56** (Images: warm ComfyUI when the tab opens, real step progress, unload immediately, in-app
fullscreen). User-facing checks still waiting: GPU guard with a real game (#1), Mac lid-closed
sleep (#2), private-repo push (#4), endpoint load test (#11), clean-machine installer (#14),
Docker Desktop after a cold reboot (#30), Xcode (#31).

## Phase 7: memory, search, schedules, documents

Built as mini-phases (`docs/phase7a-results.md`, `phase7b-results.md` for 7b+7c, `phase7d-results.md`,
`phase7e-results.md`).

- Session search (`search:`): SQLite FTS5 over every session's messages, tool calls and output. Search box on the
  session list (`GET /search`); agents get `session_search` and `session_read`.
- Memory writes (`memory_library.writes`): `memory_edit` / `memory_write` in the readable categories. Every change asks,
  with the diff on the phone; once approved, the daemon's clone commits and pushes. Sensitive categories stay invisible.
- Agent profile (`memory_library.profile_path`, `agent-profile.md` in the library): a short curated file put into each
  new session's system prompt once, so the prompt prefix stays cacheable. Settings → Memory shows it.
- Scheduled jobs (`jobs:`): cron schedules for agent tasks, managed under ⏰ Jobs. Runs end with `STATUS: OK` or
  `STATUS: ATTENTION`; OK is quiet or low-priority per job, ATTENTION notifies normally.
- Documents and a recorded web (`web:`): `web_fetch` reads PDFs (page-marked) and Word `.docx`, not only HTML and text.
  `harness/web_fixture.py` records real searches and pages; `web.fixture_dir` replays them without the network, and
  `python -m bakeoff.web_suite` runs research tasks on that recording.

## Phase 6: extensions

Built as mini-phases: 6a Hermes Agent study (`docs/phase6a-hermes-study.md`), 6b web search (`docs/phase6b-results.md`),
then 6c inference endpoint, 6d image generation, 6e distributable daemon.

- Web (`web:` in `config/harness.yaml`): `web_search` via SearXNG (`D:\Docker\searxng`, 127.0.0.1:8888) and `web_fetch`
  (public addresses only, redirects re-checked, 15K characters per call, `find` for passages). No approval needed;
  projects opt out with `web: false`.
- Inference endpoint (`endpoint:`; `docs/phase6c-results.md`): OpenAI- and Anthropic-compatible proxy to the tower's
  model at `https://<tower>/v1` with per-device keys (Settings → Inference endpoint). Requests go ahead of the next
  agent turn; 503 while the GPU guard has the model unloaded. `/v1/embeddings` is advertised when a separate
  llama.cpp embedding server/model is configured.
- Images (`images:`; `docs/phase6d-results.md`): ComfyUI (`C:\AI\ComfyUI`, started on demand) with Z-Image-Turbo (`fast`),
  Qwen-Image-2512 (`quality`), optional Lightning 4-step `quality-fast`, and optional FLUX.2 klein 4B FP8 `flux-fast`,
  all Apache 2.0. Optional modes stay disabled until their pinned files and preflight checks succeed. A batch unloads the
  language model, generates, and restores it. Phone: Images screen; agents: `generate_image` (tower and MacBook sessions).
  Opt-in Real-ESRGAN 2×/4× upscaling preserves the original PNG. Optional `image_edit` adds owner-only masked
  inpainting with Qwen-Image-Edit and is not downloaded unless that component is enabled (`docs/INSTALL.md`).
  Edits use a 1664 px / `images.max_pixels` envelope: uploads downscale to it, over-size gallery sources (including
  upscales) are rejected.
- Distributable (`docs/phase6e-results.md`): app API `/api/v1` with scoped tokens, context, app-registered tools and
  events (`harness/apps.py`, `sdk/harness_client.py`); installer, uninstaller and `python -m harness.doctor`.

## Phase 5: operations hardening

GPU contention guard, metrics and dashboard, nightly backups, read-only memory library for agents.
Details and verification: `docs/phase5-results.md`.

- GPU guard (`gpu_guard:` in `config/harness.yaml`): while a game (Steam/Epic/GOG/Xbox library executable, or the
  Steam Big Picture window) or a Plex hardware transcode runs, the queue pauses after the current model turn and
  llama-server is stopped (pause flag `C:\AI\llama-server.paused`, honored by `ops/llama-server/run-qwen.ps1`). It
  reloads after 3 min clear. Settings → GPU pauses or resumes by hand. API: `GET /gpu`, `POST /gpu/{pause|resume}`.
- Metrics: `GET /metrics`, scraped as Prometheus job `agent_harness`; Grafana dashboard "Agent Harness".
- Backups (`backup:`): nightly dated database/transcript snapshots, 14 days; `POST /maintenance/backup`. Generated
  PNGs are verified into the separate `images/YYYY/MM` archive once, with JSON metadata. Image retention defaults to
  indefinite (`image_archive_keep_days: 0`) and deletion requires an owner preview and apply action.
- Memory library (`memory_library:`; repo URL in `harness.local.yaml`): tools `memory_index`, `memory_search`,
  `memory_read` over allowlisted categories of a daemon-owned clone. Projects opt out with `memory_library: false`.
- Homelab: `rebuild_service` (`docker compose up -d --build`, always asks).
- After changing `run-qwen.ps1`, restart the whole `AgentHarness-LlamaServer` task (the supervisor reads the script once).

## Phase 4: MacBook target

Projects with `target: macbook` run their tools on the MacBook; the model and agent loop stay on the tower.
Details and verification: `docs/phase4-results.md`.

- Agent Harness Runner: `macrunner/` (stdlib-only Python 3.9, launchd agent). It connects out to Agent Harness Server over the tailnet and
  long-polls `POST /runners/macbook/poll` with a bearer token. Shell commands run natively under `sandbox-exec`
  (`macrunner/sandbox.sb`): writes limited to the workspace, temp and build caches; credentials and personal
  folders unreadable; no network unless the command was approved with `network: true`. Binary files generated on
  the tower (`generate_image`) are copied into the Mac workspace with a `put_file` op (base64 in the poll request).
- Install or update Agent Harness for Mac from **Settings → Apps → Pair Agent Harness for Mac**. Run the one-time
  command on the Mac; it creates a venv and Agent Harness CLI under `~/.agent-harness`, pairs without SSH or token copying, and installs the Mac Runner as
  a launchd agent. `harness projects add ~/Projects/<repo>` extends its allowed roots; `harness runner
  status|restart|logs` manages it locally. The older `.\ops\macbook\deploy.ps1` SSH flow remains an update fallback.
- First-party clients use an explicit current-plus-previous protocol contract. `harness version` shows the installed
  Mac client and connected Server; `harness update` performs a hash-verified transactional update without replacing
  credentials or configuration. See [`docs/compatibility.md`](docs/compatibility.md).
  See [`docs/mac-client.md`](docs/mac-client.md).
- Sessions for an offline or sleeping Mac wait (`waiting_target`) without holding the GPU, notify, and resume when
  the runner reconnects. While a Mac session runs, the runner holds `caffeinate -i`.
- `GET /runners` shows runner state; so does the Settings screen's Disk card.

## Phase 3: tower projects and homelab tasks

Git-backed projects with a branch per session, allowlisted homelab tools, cleanup and quotas.
Details and verification: `docs/phase3-results.md`.

- Projects: checked-in defaults live in `config/projects.yaml` (`repo`, `base_branch`, `homelab`, `quota_mb`). The
  owner can add private projects from **New task → New project**; they persist in `<data_dir>/projects.yaml` and hot-
  load without changing the public config. Review a git project's session branch on the Changes tab: merge (local
  repos), push (URL repos), or discard. Guest/demo logins receive empty project and session views.
- Homelab allowlist, cleanup and quota settings: `homelab:` and `cleanup:` in `config/harness.yaml`.
- API: `POST /sessions/{id}/review/{merge|push|discard}`, `GET /maintenance`, `POST /maintenance/cleanup`.
- After code or config changes: `.\ops\harness\restart-daemon.ps1`.

## Phase 2: phone control surface

Agent Harness Server serves Agent Harness Web (an installable PWA) and sends phone notifications through a self-hosted ntfy.
Details, security model, and the exit-test checklist: `docs/phase2-results.md`.

- Autostart: logon task `AgentHarness-Daemon` (`ops/harness/install-task.ps1`), logs in `D:\Agents\harness\logs`.
- Tailnet: `ops/tailscale/serve.ps1` publishes `https://tower.your-tailnet.ts.net` (daemon), `:8443` (ntfy), and `:3000` (Grafana dashboard **Agent Harness**, uid `agent-harness`, `https://<tower>.ts.net:3000/d/agent-harness/agent-harness`).
- Notifications: `notify` in `config/harness.yaml`; ntfy lives in `D:\Docker\ntfy`.
- Screenshots: `node scripts/ui-shot.mjs runs/shots "list=http://127.0.0.1:8100/#/"`.

## Phase 1: Agent Harness Server

Agent Harness Server runs agent sessions against the always-on model server, one sandbox container per session.
Design notes and test results: `docs/phase1-results.md`.

```powershell
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m harness                      # daemon on 127.0.0.1:8100 (config/harness.yaml)
.\.venv\Scripts\python -m harness.cli new "Clone local:invoice-tools, fix the failing test, and report back"
.\.venv\Scripts\python -m harness.cli list             # also: watch, send, approve, deny, cancel, transcript, queue
.\.venv\Scripts\python -m pytest tests -q -n 4 --dist loadfile   # CI-shaped parallel run (install pytest-xdist extra)
.\.venv\Scripts\python -m pytest tests -q -p no:xdist            # serial escape hatch (also `-n 0`)
```

- Data: `D:/Agents/harness` (SQLite DB, per-session workspaces, Markdown transcripts). `git_clone local:<name>`
  clones from `D:/Agents/repos`.
- Projects and approval rules: `config/projects.yaml`. Defaults ask before `git push`, destructive git commands,
  network commands, and deletes outside scratch paths.
- API: `POST /sessions`, `GET /sessions[/{id}]`, `POST /sessions/{id}/messages`, `GET /sessions/{id}/events`
  (SSE, resume with `?after=<seq>`), `POST /sessions/{id}/approvals/{approval_id|pending}`,
  `POST /sessions/{id}/cancel`, `GET /sessions/{id}/transcript`, `GET /queue`.

## Phase 0: model bake-off

- Runtime: llama.cpp `llama-server` b10950 (CUDA 13.3) at `C:\AI\llama.cpp\b10950`, models in `C:\AI\models`.
- Profiles: `bakeoff/models.yaml`. Every server binds to 127.0.0.1.
- Sandbox: `sandbox/Dockerfile`, one container per task, no network, only the workspace mounted.
- Tasks: `bakeoff/tasks.py`, 13 scripted agent tasks with automatic checkers.

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m bakeoff.run --selftest     # verify checkers (no model needed)
.\.venv\Scripts\python -m bakeoff.run --repeats 2    # full bake-off; report in runs/<timestamp>/summary.md
.\.venv\Scripts\python -m bakeoff.run --suite hard --repeats 2 --skip-perf   # 10 harder tasks with hidden checks
```

- `--suite core` (default) is the original 13 tasks. `--suite hard` (`bakeoff/tasks_hard.py`) exists because Qwen3.6
  scored 26/26 on core. Hard tasks get 50 turns and 30 minutes each.

## D1 reference harnesses: OpenHands and OpenCode

`bakeoff/reference.py` runs the same tasks and local model through an existing harness and grades them with the
same checkers. Versions are pinned in `reference/<harness>/Dockerfile` (OpenHands CLI 1.16.0, OpenCode 1.18.30).

```powershell
.\.venv\Scripts\python -m bakeoff.reference --harness opencode --suite hard --models qwen3.6-35b-a3b,gpt-oss-20b --repeats 2
```

These harnesses execute commands wherever they run, so each runs in a container on the internal Docker network
`harness-llm`. The container's only route out is the `harness-llm-proxy` socat container, which forwards to
llama-server on the host. Internet, LAN, and other host ports are unreachable. OpenCode's web tools and its
ask-the-user tool are denied in `reference/opencode/opencode.json`.

Long runs can exhaust free RAM (llama-server with Qwen holds 14–19 GB) and trip a watchdog in the launching tool.
`scripts/run-detached.ps1` runs them as a Task Scheduler job instead:

```powershell
.\scripts\run-detached.ps1 -Log runs\opencode-console.log -PythonArgs '-m bakeoff.reference --harness opencode --suite hard --models qwen3.6-35b-a3b,gpt-oss-20b --repeats 2 --no-build'
```

The agent loop in `bakeoff/agent.py` is intentionally minimal. It is the baseline the Phase 1
daemon has to beat, and the reference point for comparing an existing harness (D1).

## Always-on model server

- `ops/llama-server/run-qwen.ps1` supervises Qwen3.6-35B-A3B on `127.0.0.1:8090` (restarts on exit, unloads after
  30 idle minutes, reloads on the next request). Logs: `C:\AI\logs\`.
- `ops/llama-server/install-task.ps1` registers the hidden per-user logon task `AgentHarness-LlamaServer`.
- Prometheus job `llama_server` and the Grafana dashboard "Local LLM (llama-server)" live in `D:\Docker\observability-stack`.
- The bake-off starts its own servers on port 8081 and needs the whole GPU, so stop the always-on server first:
  `Stop-ScheduledTask AgentHarness-LlamaServer; Stop-Process -Name llama-server` (then `Start-ScheduledTask AgentHarness-LlamaServer`).
