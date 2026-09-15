# agent-harness

Personal agent harness for `dflippotower`: agents run on the basement PC against local models
and are driven from the phone or MacBook over Tailscale. The phased plan lives in the agent
memory library (`categories/project-ideas/capsules/local-agent-harness.md`).

## Phase 6: extensions (current)

Built as mini-phases: 6a Hermes Agent study (`docs/phase6a-hermes-study.md`), 6b web search (`docs/phase6b-results.md`),
then 6c inference endpoint, 6d image generation, 6e distributable daemon.

- Web (`web:` in `config/harness.yaml`): `web_search` via SearXNG (`D:\Docker\searxng`, 127.0.0.1:8888) and `web_fetch`
  (public addresses only, redirects re-checked, 15K characters per call, `find` for passages). No approval needed;
  projects opt out with `web: false`.
- Inference endpoint (`endpoint:`; `docs/phase6c-results.md`): OpenAI- and Anthropic-compatible proxy to the tower's
  model at `https://<tower>/v1` with per-device keys (Settings → Inference endpoint). Requests go ahead of the next
  agent turn; 503 while the GPU guard has the model unloaded.

## Phase 5: operations hardening

GPU contention guard, metrics and dashboard, nightly backups, read-only memory library for agents.
Details and verification: `docs/phase5-results.md`.

- GPU guard (`gpu_guard:` in `config/harness.yaml`): while a game (Steam/Epic/GOG/Xbox library executable, or the
  Steam Big Picture window) or a Plex hardware transcode runs, the queue pauses after the current model turn and
  llama-server is stopped (pause flag `C:\AI\llama-server.paused`, honored by `ops/llama-server/run-qwen.ps1`). It
  reloads after 3 min clear. Settings → GPU pauses or resumes by hand. API: `GET /gpu`, `POST /gpu/{pause|resume}`.
- Metrics: `GET /metrics`, scraped as Prometheus job `agent_harness`; Grafana dashboard "Agent Harness".
- Backups (`backup:`): nightly to `D:/My Backups/agent-harness/<date>`, 14 days; `POST /maintenance/backup`.
- Memory library (`memory_library:`; repo URL in `harness.local.yaml`): tools `memory_index`, `memory_search`,
  `memory_read` over allowlisted categories of a daemon-owned clone. Projects opt out with `memory_library: false`.
- Homelab: `rebuild_service` (`docker compose up -d --build`, always asks).
- After changing `run-qwen.ps1`, restart the whole `AgentHarness-LlamaServer` task (the supervisor reads the script once).

## Phase 4: MacBook target

Projects with `target: macbook` run their tools on the MacBook; the model and agent loop stay on the tower.
Details and verification: `docs/phase4-results.md`.

- Runner: `macrunner/` (stdlib-only Python 3.9, launchd agent). It connects out to the daemon over the tailnet and
  long-polls `POST /runners/macbook/poll` with a bearer token. Shell commands run natively under `sandbox-exec`
  (`macrunner/sandbox.sb`): writes limited to the workspace, temp and build caches; credentials and personal
  folders unreadable; no network unless the command was approved with `network: true`.
- Install or update from the tower: `.\ops\macbook\deploy.ps1 -MacHost <host> -MacUser <user>` (needs Remote Login
  on the Mac while deploying). Daemon side: `runners:` in `config/harness.yaml`; runner side:
  `~/.agent-harness/runner/config.json` (`repo_roots` limits which Mac repos projects may use).
- Sessions for an offline or sleeping Mac wait (`waiting_target`) without holding the GPU, notify, and resume when
  the runner reconnects. While a Mac session runs, the runner holds `caffeinate -i`.
- `GET /runners` shows runner state; so does the Settings screen's Disk card.

## Phase 3: tower projects and homelab tasks

Git-backed projects with a branch per session, allowlisted homelab tools, cleanup and quotas.
Details and verification: `docs/phase3-results.md`.

- Projects: `config/projects.yaml` (`repo`, `base_branch`, `homelab`, `quota_mb`). Review a session's branch on the
  Changes tab: merge (local repos), push (URL repos), or discard.
- Homelab allowlist, cleanup and quota settings: `homelab:` and `cleanup:` in `config/harness.yaml`.
- API: `POST /sessions/{id}/review/{merge|push|discard}`, `GET /maintenance`, `POST /maintenance/cleanup`.
- After code or config changes: `.\ops\harness\restart-daemon.ps1`.

## Phase 2: phone control surface

The daemon serves a mobile web app (installable PWA) and sends phone notifications through a self-hosted ntfy.
Details, security model, and the exit-test checklist: `docs/phase2-results.md`.

- Autostart: logon task `AgentHarness-Daemon` (`ops/harness/install-task.ps1`), logs in `D:\Agents\harness\logs`.
- Tailnet: `ops/tailscale/serve.ps1` publishes `https://tower.your-tailnet.ts.net` (daemon) and `:8443` (ntfy).
- Notifications: `notify` in `config/harness.yaml`; ntfy lives in `D:\Docker\ntfy`.
- Screenshots: `node scripts/ui-shot.mjs runs/shots "list=http://127.0.0.1:8100/#/"`.

## Phase 1: harness daemon

The daemon runs agent sessions against the always-on model server, one sandbox container per session.
Design notes and test results: `docs/phase1-results.md`.

```powershell
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m harness                      # daemon on 127.0.0.1:8100 (config/harness.yaml)
.\.venv\Scripts\python -m harness.cli new "Clone local:invoice-tools, fix the failing test, and report back"
.\.venv\Scripts\python -m harness.cli list             # also: watch, send, approve, deny, cancel, transcript, queue
.\.venv\Scripts\python -m pytest tests                 # scripted-model tests; one needs Docker, none need the GPU
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
