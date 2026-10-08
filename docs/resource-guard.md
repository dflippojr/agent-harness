# Resource guard (#311)

The tower has 31.8 GB of RAM and a 16 GB GPU, shared with Docker/WSL, Plex, games, several Claude/Codex sessions and a
browser. The local model (Qwen3.6-35B-A3B, a MoE whose expert tensors stay in system RAM) costs about 14 GB of RAM when
loaded. The resource guard (formerly the GPU guard, `harness_modules/local_model/service.py`) makes the harness share that better. It
covers the VRAM triggers (games, Plex transcodes), a free-memory threshold, the manual hold, and manual load/unload in
one place: **Actions → Resources**.

## When the model loads

llama-server loads the model whenever its process starts, so the harness controls loading through the supervisor's
pause flag (`gpu_guard.pause_flag`, `C:/AI/llama-server.paused`). While the flag exists the supervisor
(`ops/llama-server/run-qwen.ps1`) doesn't start the server. The model is **parked**: unloaded, with the server
process not running.

The model loads only on real demand or a clear signal:

| Signal | What happens |
| --- | --- |
| A local-model turn needs it | `Runner._model_call` → `ModelWarmer.ensure_loaded` removes the flag and waits for `/health`. The session shows the usual "waking" note (about a minute). |
| An endpoint request (`/v1/...`) | Same, before the request is relayed, on every route that goes to llama-server (`/v1/messages/count_tokens` too, though it takes no GPU slot). Below the RAM threshold it gets a 503 with `Retry-After: 60`. A parked model that can't load because the GPU is held gets a 503 instead of a 502 from the stopped server (`Retry-After: 180`, or 60 for an image batch). |
| Work was queued during a GPU hold | The hold's end reloads the model (`guard.want_model`), as before. |
| The user picks the local model in the app | Choosing the local backend or a model, or typing a task with it selected, calls `POST /models/warm`. That is skipped (`low_memory`) when RAM is short. |
| **Load local model now** (Actions → Resources) | Loads the model and pins it for the chosen window (default `load_now_default_minutes`, 60). A one-token request every `keepalive_seconds` (300) keeps llama-server's idle timer from firing. Under memory pressure it asks for confirmation first. |

These **don't** load the model:

- **A GPU hold ending, manual or automatic, with nothing queued.** The flag stays and the Actions tab offers *Load
  local model now*. (Before this change, every game, Big Picture or Plex-transcode resume loaded Qwen. On 10/1 one
  load got zero requests and cost about 14 GB for 30 minutes.)
- **Images giving the GPU back.** This was verified: `ImageService._restore_after_batch` used to remove the flag after
  every batch. It now leaves the model parked unless the model is pinned.
- **Opening the app, switching tabs, saving or deleting a template.** The web app no longer calls `warmModel()` on
  boot, on `visibilitychange` or after template changes.
- **A daemon restart.** A flag without a persisted manual hold now means "parked", not "paused".
- **Logon (tower).** `run-qwen.ps1` parks the server at logon and the daemon loads it on demand.
  `-LoadAtLogon` restores the old eager start. *Decision:* lazy by default. The daemon is the model's only consumer
  on the tower (Grafana only scrapes `/metrics`, which is simply down while parked). Installed instances
  (`install/run-server.ps1`, `run-server.sh`) still start eagerly at logon. Their daemons are lazy after holds.
- **A GPU hold, image batch or Unload now that starts during a load.** Every load removes the flag through
  `ModelWarmer._unpark`, and every path that stops the server (the hold, an image batch, Unload now) goes through
  `ModelWarmer.park`. Park aborts a load in flight rather than waiting it out: the load puts the flag back as soon as
  its unlink has landed and returns. Park then writes the flag again and stops llama-server, and no new load starts
  until it is done, so the supervisor can't start llama-server while ComfyUI or a game owns the card or after an
  unload. (`run-qwen.ps1` writes the flag at logon, before the daemon runs; llama-server's own idle sleep doesn't
  touch the flag.)
- **A llama-server that is starting but not listening yet (#344).** `ServerControl.stop()` writes the flag, then stops
  every process that holds the port *and* every `llama-server` process whose command line says `--port <our port>`
  (psutil name and command-line match; no `--port` means llama-server's default, 8080). So a server `run-qwen.ps1`
  launched while the flag was briefly missing, and that is still loading, dies with the rest instead of finishing its
  load while ComfyUI owns the GPU. *Decision:* match by process in the daemon rather than have the supervisor re-check
  the flag after launching; the supervisor is unchanged. The flag is written first, so it won't start another. A
  server started in the instant between the process listing and the supervisor's flag check is the only window left.

`lazy_load: false` in `config/harness.yaml` restores the old eager reload after holds and image batches.

**Unload now** (Actions → Resources, `POST /resources/unload`) parks the model straight away without holding the queue.
It refuses (409) while a model turn is running or loading the model for one: the turn is in `Runner.generating` from
before it unparks the model until its call ends, and that is the lease the unload checks. A refused unload leaves a
*Load local model now* pin and its keepalive alone. llama.cpp b10950 has no sleep endpoint: `--help` lists only
`--sleep-idle-seconds`. So unloading means stopping the process, the same way the hold does.

## Idle unload: 600 s

`--sleep-idle-seconds` changed from 1800 to **600** in `run-qwen.ps1`, `install/run-server.sh` and the installer's
`settings.json` default. The trade-off is a ~60 s wake after 10 idle minutes instead of 30. An active *Load local model
now* window overrides it.

**Paired change outside this repo:** the `sleep_after` constant in the Grafana "Local LLM (llama-server)" dashboard
lives in the observability-stack repo and must change from 1800 to 600 when this deploys.

## RAM threshold

The guard's `MemoryWatch` reads available physical memory (psutil, plus Windows commit via `GlobalMemoryStatusEx`).
Below `min_available_ram_gb` (default **4**, `0` turns it off), the harness:

- doesn't load the model, whether parked or asleep. A session emits `waiting_memory`, which shows in the session and on
  the phone. It emits `memory_recovered` when memory frees up, and then the turn continues.
- doesn't start new hosted-CLI worker containers (Claude/Codex/Cursor). Each attempt waits in `Runner._run_cli`
  before `_start_cli`, with the same events. This replaces the informal "max two workers because of memory" rule.
- doesn't start ComfyUI jobs. The image batch stops llama-server first, which frees its memory, then waits in phase
  `waiting_memory` before starting ComfyUI.
- skips selection warm-ups, and asks before *Load local model now*.

A model load is held to a stricter test than the rest. Loading adds about `model_ram_gb` (default **14**; Phase 0
measured 12.5 to 14.7 GB private bytes) to physical use, so a load starts only when available RAM **minus
`model_ram_gb`** is still at or above `min_available_ram_gb`. With 11.7 GB free the load waits (it would leave nothing);
with 18.5 GB free it goes ahead. Before this, the daily 08:00 load saw 11.7 GB free against the 4 GB threshold, passed,
and then pushed available RAM to 0.17 GB for 2 to 4 minutes. Worker containers and ComfyUI jobs still compare the plain
number. The wait polls every `MEMORY_POLL_SECONDS` and the session shows `waiting_memory` with the reason
"the model load needs 14 GB and 4 GB must stay free".

Work already running isn't stopped. If nothing can be read (no psutil), work isn't blocked.

### Inventory (read-only, 2026-10-06)

Measured from Prometheus (`windows_memory_available_bytes`, `harness_resource_*`), the daemon log, `Get-Counter` and
`nvidia-smi`, with nothing changed on the tower.

- **Idle, model unloaded (07:20).** Physical 31.8 GB, about 13 GB free. Docker VM (`vmmemWSL`) 5.75 GB working set under
  the 8 GB `.wslconfig` cap; containers use about 1.6 GB and the rest is page cache (VM `Cached` 4.38 GB, swap 217 MB).
  Containers: grafana 513 MB, prometheus 167 MB, financial-planner-app 166 MB, tempo 80 MB, harness-eulogin 74 MB, the
  rest 1 to 60 MB. Host processes by working set: claude (7) 2.1 GB, Memory Compression 1.3 GB, powershell (18) 1.0 GB,
  Defender 0.95 GB, Orca (11) 0.9 GB, msedge (12) 0.8 GB, Plex 60 MB. VRAM 529 MiB used. Committed 31.1 GB of a 63.8 GB
  limit (pagefile 32 GB, 5 GB peak use).
- **Peak, 14 days.** `harness_resource_commit_bytes` peaked at 55.8 GB of 63.8 GB (p50 26.8 GB);
  `windows_memory_available_bytes` reached 0.16 GB. Of 10,072 two-minute samples, 46 had under 1 GB available, 86 under
  2 GB and 159 under 4 GB. One 50-minute period (2026-10-05 23:50, commit 52 GB, model loaded, no active sessions) had 0.5
  to 5.3 GB available; that consumer is not a harness session and is not attributed.
- **The 08:00 load.** On 9 of 14 mornings a 2 to 4 minute dip to 0.16 to 0.32 GB available starts at 08:01. On 2026-10-05:
  11.7 GB at 08:00, 0.173 GB at 08:02, 11.1 GB at 08:05; the normal-priority standby cache fell from 8.0 GB to 0.5 GB.

Method for the morning check: query `windows_memory_available_bytes` at 2-minute steps from 08:00 to 08:10 each day and
count samples under 1 GB. The target after this change is 0 over 7 mornings.

## Diagnostics and metrics

Actions → Resources shows a diagnostics card styled like Actions → Disk:

- VRAM used and total, plus which harness process holds it
- RAM available, total and commit, plus the harness's share (daemon, llama-server, `harness-*` containers)
- GPU load and CPU load
- model state (unloaded, waking, loaded, or "loaded until HH:MM")
- guard state and its reasons

It takes **one reading when the tab opens**, shows "as of HH:MM", and re-reads only when you press ↻. Nothing polls
in the background. API: `GET /resources/diagnostics`.

`/metrics` exports the following for Grafana's own scrape (including `harness_model_load_min_available_bytes`, the lowest
available RAM sampled during the most recent model load, every 2 s while it waits for `/health`):

- `harness_resource_ram_available_bytes`, `_ram_total_bytes`, `_commit_bytes`, `_commit_limit_bytes`,
  `_ram_threshold_bytes`
- `harness_resource_memory_low`, `harness_model_parked`, `harness_model_pinned_until_seconds`
- `harness_resource_vram_used_bytes` / `_vram_total_bytes` / `_gpu_load_percent` (from nvidia-smi, cached for 10 s)
- `harness_resource_cpu_load_percent`

The `harness_gpu_guard_*` series are unchanged.

## Naming

The owner proposed **Resource guard**, with "Headroom guard" and "Machine guard" as alternatives to pick from at
review. The UI copy, the Actions tab (`#/actions/resources`; `#/actions/gpu` redirects) and the API (`/resources`, with
`/gpu` kept as an alias) use the new name. The `gpu_guard:` YAML section, the `gpu_guard` module switch, `GpuGuard`
and the `harness_gpu_guard_*` metrics keep their names until the name is final. Settings writes target
`gpu_guard.*` paths, so a second YAML alias would silently shadow them.

## Memory cost of the loaded model

`ops/llama-server/measure-memory.ps1 -Label <name> [-Bench]` records one row of system counters to
`C:\AI\logs\memory-measurements.jsonl`: available, committed, commit limit, the standby lists and modified pages. It
also records llama-server's private bytes and working set, and with `-Bench` the server's own prompt/decode tokens/s
for a fixed prompt. It changes nothing on the machine.

### Baseline (2026-10-02 16:56, model parked)

| Counter | GB |
| --- | --- |
| Available | 7.5 |
| Committed / limit | 40.3 / 63.8 |
| Standby (normal priority) | 7.0 |
| Modified page list | 0.1 |
| llama-server | not running (parked) |

### A/B: default mmap vs `--load-mode none` (#405, 2026-10-06 20:45)

Measured on a throwaway llama-server on port 8097 with the same b10950 binary, GGUF and `run-qwen.ps1` args. The
GPU hold was on, production was parked, and Docker/WSL was stopped for the night (20.4 GB available before each load).
`ops/llama-server/bench-first-request.ps1` waits for `/health` and takes a `measure-memory.ps1` row. It then sends a
fixed 6,360-token excerpt of `docs/*.md` (6,372 prompt tokens with the template) as the first request, with no prompt
cache, and sends it again as the second request. A watchdog stops the server below 2 GB available.

| | mmap (default `auto`) | `--load-mode none` (2 runs) |
| --- | --- | --- |
| Load to `/health` | 39.5 s (38.3 s) | 14.2 s, 14.2 s |
| Available while loaded | 6.57 GB | 12.08, 11.91 GB |
| Lowest available during the run | **1.73 GB, then the watchdog stopped it** (0.61 GB in an earlier run with 14.8 GB free) | 11.55, 11.35 GB |
| Committed / limit | 33.1 / 63.8 GB | 41.4 / 63.8 GB |
| llama-server private / working set | 15.07 / 13.87 GB | 23.36 / 8.86 GB |
| First request, 6,372 tokens | not finished: available fell from 6.6 to 1.7 GB in 6 s while it paged experts in | **5.66 s at 1,125 tok/s**, 5.33 s at 1,195 tok/s |
| Second request, same prompt | n/a | 1,156 and 1,232 tok/s |
| Decode | 56-70 tok/s (production logs) | 64-74 tok/s |

Production's own first requests after a load, from `C:\AI\logs\llama-server-qwen.log*` with the method in #405 (mmap):
6,388 tokens in 42.8 s at **149 tok/s** (2026-10-06 08:00, load 72.5 s), 36.9 s at 173 tok/s (10-05) and 45.8 s at
139 tok/s (10-04).

What the numbers say: with mmap, `--fit` reads the GPU tensors through the mapping, and those file pages stay in the
working set (13.9 GB at `/health`). The first prompt then faults in the CPU expert pages on top of that. That is the
slow first prompt and the 08:00 dip to 0.17 GB. With `none` the GPU tensors are copied and released, and only the CPU
part (~9 GB) stays resident.

mmap plus a warm-up request was not run. The warm-up is the same page-in that took available RAM under the 2 GB floor
in the mmap run, so it would only move the 40 s, not remove it.

b10950's `--load-mode` accepts `auto` (mmap unless a device can't), `none`, `mmap`, `mlock`, `mmap+mlock` and `dio`.
The interim decision (#311) kept `auto` because mmap'd expert pages are file-backed and Windows can drop them under
pressure. The numbers above overturn it.

**Decision: `--load-mode none`** (in `run-qwen.ps1`). It is clearly faster (first prompt 7-8x, load 2.8x) and leaves
about 5.5 GB *more* RAM available while loaded. The cost is commit: about 8.3 GB more (private 23.4 GB, of which only
8.9 GB is resident). Over 14 days commit peaked at 55.8 of 63.8 GB with the mmap model. A similar peak with `none`
would reach the commit limit, so watch `harness_resource_commit_bytes` and enlarge the page file if it gets close.
The installers (`install/`) keep the default; they weren't measured on those machines.

The guard is unchanged. A load still needs available RAM minus `model_ram_gb` (14) at or above
`min_available_ram_gb` (4). With `none` a load actually takes about 9 GB of available RAM, so 14 stays a
conservative estimate. A load that would leave less than 4 GB still waits.
