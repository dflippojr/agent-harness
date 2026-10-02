# Resource guard (#311)

The tower has 31.8 GB of RAM and a 16 GB GPU, shared with Docker/WSL, Plex, games, several Claude/Codex sessions and a
browser. The local model (Qwen3.6-35B-A3B, a MoE whose expert tensors stay in system RAM) costs about 14 GB of RAM when
loaded. The resource guard (formerly the GPU guard, `harness/gpu_guard.py`) makes the harness share that better. It
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

Work already running isn't stopped. If nothing can be read (no psutil), work isn't blocked.

## Diagnostics and metrics

Actions → Resources shows a diagnostics card styled like Actions → Disk:

- VRAM used and total, plus which harness process holds it
- RAM available, total and commit, plus the harness's share (daemon, llama-server, `harness-*` containers)
- GPU load and CPU load
- model state (unloaded, waking, loaded, or "loaded until HH:MM")
- guard state and its reasons

It takes **one reading when the tab opens**, shows "as of HH:MM", and re-reads only when you press ↻. Nothing polls
in the background. API: `GET /resources/diagnostics`.

`/metrics` exports the following for Grafana's own scrape:

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

### A/B: default mmap vs `--load-mode none` (to run)

b10950's `--load-mode` accepts `auto` (mmap unless a device can't), `none`, `mmap`, `mlock`, `mmap+mlock` and `dio`.
To compare:

1. With the queue idle, take `measure-memory.ps1 -Label mmap-parked`.
2. Load the model (Actions → Resources → Load local model now), wait for "Loaded", then run
   `-Label mmap-loaded -Bench`.
3. Add `'--load-mode', 'none'` to `$serverArgs` in `run-qwen.ps1`, unload, and repeat as `none-parked` and
   `none-loaded -Bench`.
4. Optionally repeat the loaded reading while something heavy runs, to see whether Windows trims the mmap working
   set.

These steps restart the live model server, so they need the owner at the tower. They weren't run unattended.

**Interim decision: keep the default (`auto` = mmap).** With mmap, the CPU-offloaded expert pages are file-backed. They
count toward the working set but not toward private commit, and Windows can drop them under pressure and re-read them
from the GGUF. With `none` the same ~14 GB becomes private memory that counts against commit and can only be paged to
the page file. So mmap should behave better under memory pressure, possibly at some decode speed when pages have been
dropped. Record the A/B numbers above and revisit this decision if `none` is markedly faster without commit trouble.
