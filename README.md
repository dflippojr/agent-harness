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
```

The agent loop in `bakeoff/agent.py` is intentionally minimal. It is the baseline the Phase 1
daemon has to beat, and the reference point for comparing an existing harness (D1).
