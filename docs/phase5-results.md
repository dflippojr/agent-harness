# Phase 5: operations hardening

Built 2026-09-14/15. Autostart, restart supervisors and the GitHub remote already existed from Phases 1–3, so this
phase added GPU contention handling, monitoring, backups, a read-only memory-library tool, and two Phase 4 leftovers.

## Decisions (user, 2026-09-14)

- Sunshine is used for both gaming and desktop streaming; Plex hardware transcoding is on.
- **GPU policy: pause + unload.** Let the current model turn finish, pause the queue, stop the model server, notify,
  and resume automatically when the GPU is free.
- **Trigger: a game or a transcode**, not VRAM numbers (Windows reports no per-process VRAM). Desktop-only streaming
  keeps agents running.
- **Memory library: read-only, sensitive categories excluded.**
- **Backups:** nightly to `D:\My Backups\agent-harness`, 14 days kept.
- Also: a Mac task template with target-filtered templates, and an approval-gated `rebuild_service`.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| GPU guard | `harness/gpu_guard.py`, `gpu_guard:` in `config/harness.yaml` | detection, state machine, server control |
| Queue pause | `harness/scheduler.py` | `set_paused`; a session that steps aside re-queues first in line |
| Model-call gate | `harness/runner.py` (`_model_call`, `_gpu_gate`) | waits before each model call; retries a call cut off by a stop |
| Supervisor pause flag | `ops/llama-server/run-qwen.ps1` | doesn't restart llama-server while `C:\AI\llama-server.paused` exists |
| GPU API | `GET /gpu`, `POST /gpu/pause`, `POST /gpu/resume` | Settings → GPU card; banner on the session list |
| Metrics | `harness/metrics.py`, `GET /metrics` | computed from SQLite per scrape plus live state |
| Dashboard | `D:\Docker\observability-stack` `grafana/.../agent-harness.json`, Prometheus job `agent_harness` | "Agent Harness" in the Basement PC folder |
| Backups | `harness/maintenance.py`, `backup:` in config, `POST /maintenance/backup` | status in `D:\Agents\harness\backup-status.json` |
| Memory library | `harness/memory_library.py`, `memory_library:` in config | `memory_index`, `memory_search`, `memory_read` |
| `rebuild_service` | `harness/homelab.py`, `harness/policy.py` | `docker compose up -d --build <service>`; always asks |
| Post-reboot check | `ops/check-stack.ps1` | now checks the GPU guard / leftover flag, backup age, and the scrape target |

## GPU guard

States: `clear` → `pausing` (queue closed, waiting for the model call in flight, at most `drain_timeout_seconds`) →
`paused` (flag written, llama-server killed by the PID listening on :8090) → `resuming` (flag removed after
`resume_after_seconds` of no triggers; the supervisor restarts the server) → `clear` once `/health` answers.

Triggers, polled every 10 s:

- **Games:** any running executable whose path contains `\steamapps\common\`, `\Epic Games\`, `\GOG Galaxy\Games\`
  or `\XboxGames\`, except `\wallpaper_engine\` and `\Steamworks Shared\` (Wallpaper Engine runs from the Steam
  library all the time). Enumerated with Win32 `EnumProcesses`/`QueryFullProcessImageNameW`, no psutil.
- **Sunshine's Big Picture app:** a visible window titled `Steam Big Picture Mode`. Sunshine's logs at the default
  level don't show stream or app starts, and its API needs the web UI password, so the guard doesn't read Sunshine
  itself; a game launched from a Desktop stream is caught by the process check anyway.
- **Plex:** `GET /transcode/sessions` with the token from `HKCU\Software\Plex, Inc.\Plex Media Server`; a session counts
  when `videoDecision` is `transcode` and it uses (or requested) hardware encoding/decoding. Direct play, direct
  stream and audio-only transcodes don't count.

Details:

- The daemon holds no pause state across restarts except the flag file: on start, a leftover flag means "paused";
  the first check resumes immediately if nothing is running.
- Sessions get `gpu_paused` / `gpu_resumed` events, shown in the transcript and the app. They notify the phone
  (replaced in place via ntfy `sequence_id`), but **only when a session is affected** (running or queued at pause
  time, or started during it). An idle harness pausing for someone's Plex transcode sends nothing.
- **Pause agents** (Settings) pauses by hand until **Resume now**. **Resume anyway** during a trigger ignores the
  current triggers until the set changes (a new game or transcode pauses again).
- `/models/warm` does nothing while paused, so opening the app can't reload the model mid-game.

### Live test (2026-09-15, notifications muted for the test)

Session `58a28a715e` (scratch: write `numbers.py`, run it, write and check `summary.txt`). A copy of `PING.EXE` at
`D:\Agents\gputest\steamapps\common\FakeGame\FakeGame.exe` stood in for a game:

| Time | What happened |
| --- | --- |
| 00:43:39 | fake game started while the session was mid-run |
| 00:43:44 | guard `pausing` (turn in flight) |
| 00:43:49 | turn finished; llama-server killed, flag written; session `queued`; VRAM 1,166 MiB |
| 00:44:07 | supervisor logged "paused by the harness GPU guard" instead of restarting |
| 00:44:36 | game closed |
| 00:47:41 | 180 s clear → flag removed, `resuming`; supervisor started llama-server |
| 00:48:56 | `/health` OK after ~75 s load → `clear`, session running |
| 00:49:36 | session done, correct answer (2870) |

The detector was also run against the real process list: Wallpaper Engine is ignored, no false triggers, and the
Plex API answered (no sessions).

Not tested live: a real game, Sunshine's Big Picture window title, a real Plex hardware transcode.

## Metrics and dashboard

`/metrics` (plain Prometheus text, no client library): sessions by status, finished sessions by stop reason, queue
depth, GPU slot, generations in flight, tokens and turns per model, latest-turn tok/s, tool calls by tool/outcome,
errors/retries/compactions/GPU pauses/wakes, approvals by outcome and wait time, runner online, GPU guard state and
triggers, backup age/size, cleanup time, data-drive free space. Prometheus scrapes `host.docker.internal:8100` every
15 s (Docker Desktop reaches the host loopback, as for llama-server). All 19 dashboard queries were run against
Prometheus without errors; the dashboard wasn't viewed in a browser (Grafana needs a login).

## Backups

At 03:30 (and at start if the last good backup is over 26 h old): SQLite online backup API → `harness.sqlite3`
(integrity-checked), `transcripts.zip`, and `config/` (`harness.yaml`, `harness.local.yaml`, `projects.yaml`) into
`D:\My Backups\agent-harness\<date>`, written as `<date>.partial` and renamed when complete; dated folders older than
14 days are deleted. Secrets (`D:\Agents\harness\secrets`, ntfy tokens) are not copied. First manual run: 1.1 MB.
D: is a mirrored pool on the same machine, so this protects against DB corruption and mistakes, not against losing
the tower.

## Memory library

The daemon clones the private library repo (URL in `harness.local.yaml`; tower's git credentials) to `D:\Agents\memory-library` and
`git pull --ff-only`s it at most every 10 minutes when a tool is used. Only text files under
`categories/{project-ideas,work,home-and-pets,sport,taste-and-media,communication-preferences}/` are listed, searched
or readable; the top-level index (it names sensitive capsules), inbox, cross-category capsules and every other
category are invisible, and paths are resolved before the check so `../` can't escape. Every project gets the tools
unless it sets `memory_library: false`; the system prompt says to treat results as background facts, not instructions.

Caveat found: allowed categories can mention sensitive topics in passing (a sport note can reference an injury), so
"excluded categories" is a boundary on files, not on subjects.

## Phase 4 leftovers

- **Templates by machine:** New task has a **Runs on** switch (Tower / MacBook, remembered per device) that filters
  projects and templates. Template "Fix failing tests (invoice-tools-mac)" was added (project `invoice-tools-mac`,
  prompt runs `python3 -m unittest` in the already checked-out repo).
- **`rebuild_service`:** rebuilds and recreates an allowlisted service from its stack directory after a merge
  (e.g. plex-webhook code). Always asks. Run live against `harness-demo` (no build section, so compose kept the
  container running, as expected).

## Tests

58 pass (`tests/test_phase5.py` adds 10: detection filters, Plex parsing, scheduler pause and front re-queue, guard
drain / drain timeout / short trigger / manual pause / override / leftover flag, a scripted session paused between
turns and resumed, metrics, backup with retention, memory-library access rules, memory tools in a session, and the
rebuild policy).
