# Resource guard (#311)

The tower has 31.8 GB of RAM and a 16 GB GPU, shared with Docker, Plex, games, several Claude/Codex sessions and a
browser. The local model (Qwen3.6-35B-A3B, a MoE whose expert tensors stay in system RAM) costs about 14 GB of RAM when
loaded. The resource guard (formerly the GPU guard, `harness_modules/local_model/service.py`) makes the harness share that better. It
covers the VRAM triggers (games, Plex transcodes), a free-memory threshold, the manual hold, and manual load/unload in
one place: **Actions → Resources**.

On Linux, how the model's VRAM can share the card with one Plex transcode and a Sunshine stream, and which guard
thresholds follow from that, is designed in `docs/gpu-vram-budget.md` (#823).

The supervisor details below (`ops/llama-server/run-qwen.ps1`, `C:/AI/...` paths, logon tasks) describe the Windows
tower. Since 2026-10-26 the tower runs Ubuntu: llama-server runs as the `agent-harness-main-llama.service` user unit
through `install/run-server.sh`, and its pause flag is `~/.local/share/agent-harness/llama-server.paused`. The
Windows text applies to a Windows boot only. The measurements further down are records from the Windows tower.

Plex detection on Linux takes no Plex credential (#830). With no registry token, the guard runs `nvidia-smi
--query-compute-apps=pid,process_name,used_memory` each poll and counts one trigger per `Plex Transcoder` process.
Plex Media Server itself doesn't count, and a software transcode isn't on the GPU, so it doesn't either. If
`nvidia-smi` fails, `plex_error` in `GET /resources` says so and `doctor` warns: the guard is blind to Plex until it
works again. Windows keeps asking Plex's API while the registry holds a token, and the two sources are never added
together. Whether an NVENC-only transcode appears in that query has not been checked against a real transcode yet;
`nvidia-smi pmon -c 1` shows per-process encoder use if it turns out not to.

Game detection is Windows-only: on Linux `process_paths()` and `window_titles()` return nothing, so a game or a
Big Picture window doesn't trigger the guard there (`docs/gpu-vram-budget.md`, follow-up 5).

## When the model loads

llama-server loads the model whenever its process starts, so the harness controls loading through the supervisor's
pause flag (`gpu_guard.pause_flag`, by default `C:/AI/llama-server.paused` on Windows). While the flag exists the supervisor
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

- **Linux page-out (#835).** On Linux, `install/run-server.sh` runs llama-server in the
  `agent-harness-<instance>-llama` Docker container. The daemon can't signal that container's root process, and
  psutil gives no pid for another user's socket. So while the container runs, the supervisor polls the pause flag every
  5 s (`HARNESS_PAUSE_POLL_SECONDS`). When the flag appears it runs `docker stop -t 10` on the container, so the VRAM
  comes free within 15 s. Then it waits, without the usual 15 s restart delay, until `ServerControl.start()` removes
  the flag. `ServerControl.stop()` on Linux finds the port's listeners with `psutil.net_connections(kind="tcp")` instead
  of `netstat -ano`, and stops a native llama-server it may signal with SIGTERM (SIGKILL after 10 s) instead of
  `taskkill`. *Decision:* the supervisor watches the flag, rather than the daemon running `docker stop`. The
  supervisor already owns the container's name and its `docker stop` trap, and the daemon then needs neither the
  instance name nor Docker access. Windows is unchanged.

- **Container health (#858).** Docker's status for `agent-harness-<instance>-llama` comes from a `--health-cmd` that
  `run-server.sh` passes: `curl -f` on `/health` at the port it started llama-server on. The image's own HEALTHCHECK
  probes port 8080, so it reported `unhealthy` while the server answered. The check's start period is 300 s, so a slow
  model load does not read as unhealthy.

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

When RAM alone keeps a wanted resume parked, the guard stays `paused` and its status names that: `reasons` holds one
entry with the key `memory_hold` and a detail that starts `memory:`, and `manual` is false. It replaces the reason that
began the pause, so the app doesn't call a memory wait "paused from the app" (#873). A hand pause or a new trigger
replaces it in turn.

Work already running isn't stopped. If nothing can be read (no psutil), work isn't blocked.

### What a load costs on the Ubuntu tower (#873, 2026-11-01)

`model_ram_gb` dates from the Windows host. On Ubuntu llama-server maps the GGUF, so most of what `VmHWM` reports (10,780
to 12,958 MiB in #827's runs) is file-backed. The split below uses existing data only: #827's runs, and Prometheus and
cAdvisor from 2026-10-26 to 2026-11-01. No model was loaded for it.

| Part of the load | Size | Source |
| --- | --- | --- |
| Anonymous memory, the part nothing can reclaim with swap full | 553 to 562 MiB in #827's runs; **1,194 MiB** peak on prod | VmRSS split; `container_memory_rss` |
| Weights the CPU reads on every token: GGUF minus the GPU buffer | 8,229 MiB at the default margin; **9,157 MiB** at `--fit-target 2048` | 21,325 MiB file; CUDA0 buffer 13,096 and 12,168 MiB |
| Mapped pages at their peak, which also include GPU layers faulted in for the upload | 9,836 MiB on prod; 11,906 MiB in #827's runs | `container_memory_mapped_file` |

**The fall in available RAM is not the cost to plan for.** On the quietest load (2026-10-30 16:45 EST, other containers
+13 MiB), the mapped pages rose 9,234 MiB and the container's anonymous memory 716 MiB, while `MemAvailable` fell only
637 MiB. It counts mapped file pages as reclaimable. Those pages include the weights the CPU reads on every token, so
reclaiming them means reading them back from disk. Other loads fell further (1.6 to 4.8 GiB), but host work moved
during those, so the falls aren't the model's alone.

**Recommendation for the tower: `model_ram_gb: 11`.** Anonymous peak plus CPU-read weights at `--fit-target 2048` is
1,194 + 9,157 = 10,351 MiB, or 10.1 GiB. Rounding up gives 11, so a load with `min_available_ram_gb: 2` needs 13 GiB
available. That is not the 8 first planned: at 10 GiB available the model alone would want all of it, with nothing
left for the 2 GiB the gate promises. It may well run there, but no measured load started below 15.4 GiB, and the
lowest available RAM during a measured decode was 12.5 GiB, so 8 is untested.

Time a load would have waited, over those six days (8,542 minutes, sampled each minute):

| `model_ram_gb` + `min_available_ram_gb` | Needs | Would wait |
| --- | --- | --- |
| 14 + 4 (today) | 18 GiB | 35.6% |
| 14 + 2 | 16 GiB | 6.8% |
| 11 + 4 | 15 GiB | 4.1% |
| **11 + 2** | **13 GiB** | **1.6%** |
| 8 + 2 | 10 GiB | 0.3% |

These values aren't applied. They go on one owner card with `--fit-target 2048` (#881). The shipped default stays 14: the
reason it was set still holds on Windows, where `--load-mode none` makes the weights private memory (see below) and
the 08:00 load once pushed available RAM to 0.17 GB.

**What `min_available_ram_gb: 2` changes for the plain check** (worker containers, ComfyUI):

- **Nothing that has happened so far.** On Ubuntu, available RAM never went below 4.34 GB in those six days, so neither
  4 nor 2 would have held anything. `harness_resource_memory_low` was last set on 2026-10-09, on the Windows host.
- **Worker containers keep less room.** Swap was full in 40.2% of those minutes, and then anonymous memory has nowhere
  to go: what's available is the whole remaining cushion. A worker's `--memory 2g` limit equals the new margin, so one
  worker at its limit could use all of it, where 4 left room for two. Workers actually peaked at 0.13 GB across 355
  containers. The check reserves nothing, so several can start at once on the same reading. Past the margin the kernel
  reclaims file pages first, the model's weights among them, so decode slows from disk reads; once those run out, the
  OOM killer acts. That follows from how the kernel reclaims memory; it wasn't provoked here. CI runners (3 to 4 GB
  each) aren't gated by this check at all.
- **ComfyUI: nothing today.** `images` is disabled in the tower's overlay, and no image job ran in the 30 days Prometheus
  keeps. If it's enabled, neither number says much: the `quality` model is 19.5 GB partly streamed from RAM, and the
  check only runs before ComfyUI starts. Its peak RAM on Ubuntu hasn't been measured, so measure it before relying on
  the check.

The load gate is where the lower number matters, and most of that gain comes from `model_ram_gb` (see the table).

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

- VRAM used and total, plus which harness process holds it, and VRAM per process from `nvidia-smi`
  (`vram.processes` in the API and `harness resources diagnostics`; the page needs a Stables release to show it, #830)
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
