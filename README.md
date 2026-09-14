# agent-harness

Personal agent harness for `dflippotower`: agents run on the basement PC against local models
and are driven from the phone or MacBook over Tailscale. The phased plan lives in the agent
memory library (`categories/project-ideas/capsules/local-agent-harness.md`).

## Phase 2: phone control surface (current)

The daemon serves a mobile web app (installable PWA) and sends phone notifications through a self-hosted ntfy.
Details, security model, and the exit-test checklist: `docs/phase2-results.md`.

- Autostart: logon task `AgentHarness-Daemon` (`ops/harness/install-task.ps1`), logs in `D:\Agents\harness\logs`.
- Tailnet: `ops/tailscale/serve.ps1` publishes `https://tower.your-tailnet.ts.net` (daemon) and `:8443` (ntfy).
- Notifications: `notify` in `config/harness.yaml`; ntfy lives in `D:\Docker
tfy`.
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
