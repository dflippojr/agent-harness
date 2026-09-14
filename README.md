# agent-harness

Personal agent harness for `dflippotower`: agents run on the basement PC against local models
and are driven from the phone or MacBook over Tailscale. The phased plan lives in the agent
memory library (`categories/project-ideas/capsules/local-agent-harness.md`).

## Phase 0: model bake-off (current)

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

## D1 reference harness: OpenHands

`bakeoff/openhands_ref.py` runs the same tasks and local model through OpenHands CLI (pinned in
`reference/openhands/Dockerfile`) and grades them with the same checkers.

```powershell
.\.venv\Scripts\python -m bakeoff.openhands_ref --suite hard --models qwen3.6-35b-a3b --repeats 2
```

OpenHands executes commands wherever it runs, so it runs in a container on the internal Docker network
`harness-llm`. The container's only route out is the `harness-llm-proxy` socat container, which forwards to
llama-server on the host. Internet, LAN, and other host ports are unreachable.

The agent loop in `bakeoff/agent.py` is intentionally minimal. It is the baseline the Phase 1
daemon has to beat, and the reference point for comparing an existing harness (D1).

## Always-on model server

- `ops/llama-server/run-qwen.ps1` supervises Qwen3.6-35B-A3B on `127.0.0.1:8090` (restarts on exit, unloads after
  30 idle minutes, reloads on the next request). Logs: `C:\AI\logs\`.
- `ops/llama-server/install-task.ps1` registers the hidden per-user logon task `AgentHarness-LlamaServer`.
- Prometheus job `llama_server` and the Grafana dashboard "Local LLM (llama-server)" live in `D:\Docker\observability-stack`.
- The bake-off starts its own servers on port 8081 and needs the whole GPU, so stop the always-on server first:
  `Stop-ScheduledTask AgentHarness-LlamaServer; Stop-Process -Name llama-server` (then `Start-ScheduledTask AgentHarness-LlamaServer`).
