# GPU VRAM budget: Qwen vs Plex NVENC and Sunshine in 16 GB (#823)

Design only. Nothing here was loaded, unloaded, paused or measured on the tower; the manual GPU hold from 2026-10-23
stays on. It feeds #811 (the guard's Plex detection on Linux) and the resource guard (`docs/resource-guard.md`).

## Problem

The tower's RTX 4070 Ti Super (16 GB) is shared by the local model, Plex hardware transcodes (NVDEC/NVENC) and
Sunshine (NVENC). The owner's notes (`/home/daniel/tower-switch-results.md` on the tower, "Known limits" and
follow-up 4; quoted in #823) say the model holds about 14.5 GB, so a transcode and a stream don't fit beside it, and the
hold keeps the model parked. On Ubuntu (2026-10-26) the model loaded in about 30 s and decoded at 85 tok/s.

Two facts shape every option below:

- **`--fit on` fills the card.** llama-server's `--fit` reads free VRAM at load and places weights until only
  `--fit-target` MiB (default 1024) stay free; the MoE expert tensors that don't fit go to system RAM
  (`docs/phase0-results.md`, "Context size test": "`--fit` keeps VRAM full"; `docs/bonsai-2-study.md`,
  `docs/mitsuba-prompt-helper-study.md`). The model's VRAM is therefore *whatever is free minus the margin*, not a
  property of the GGUF. A smaller quant or a smaller context alone frees nothing; the margin (or a fixed placement) is the lever. Only `--fit-target` decides
  the headroom.
- **Linux doesn't page VRAM.** On Windows, WDDM could spill a late allocation to system memory. On Linux a CUDA or
  NVENC allocation that doesn't fit fails. Plex then falls back to software transcoding or the stream fails, and
  Sunshine can't start its encoder. llama.cpp allocates its weights, KV cache and compute buffers at load and doesn't
  grow afterwards, so the model is never the one that fails; the late arrival is.

## Where the numbers come from

| Source | Status |
| --- | --- |
| Earlier studies in this repo (Windows, same card, same GGUF) | read; cited per row |
| #823's own figures from the owner's Ubuntu notes (14.5 GB, 30 s, 85 tok/s) | quoted, not re-read |
| `/home/daniel/tower-switch-results.md` | not readable from the Razer (no SSH to the tower) |
| Prometheus on the tower | not reachable over the tailnet (`:9090` refused) |
| Grafana `nvidia-gpu` / `llama-server` dashboards | reachable, but the API needs a login (401); not read |
| Plex and Sunshine VRAM per session | never measured in this repo; public reports only, marked **estimate** |
| The tower's `agent-harness-main-llama.service` arguments | not read; assumed to be `install/install.sh`'s Qwen defaults (`--fit on --cache-type-k q8_0 --cache-type-v q8_0`, ctx 65536, so the default 1024 MiB margin) |

## The budget (MiB)

| Item | Value | Source |
| --- | --- | --- |
| Card total | 16,376 | **estimate**: the size `nvidia-smi` reports for 16 GB Ada cards; the tower's value wasn't read (`hcli doctor` prints it) |
| Desktop session, idle, model unloaded | 600 (range 300 to 700) | Windows measured 529 (`docs/resource-guard.md`, inventory 2026-10-06), 545 (`docs/mitsuba-prompt-helper-study.md`) and ~690 (`docs/qwen38-distill-study.md`). Ubuntu's GNOME session: **estimate**, not measured |
| Model, total, at 64K, default margin | 14,000 to 14,850 | ~14,150 for the server alone (`docs/qwen38-distill-study.md`, 14,658 total minus ~690 desktop, 32K); 14,712 total at 64K (`docs/phase0-results.md`); ~14,850 on Ubuntu (#823's "about 14.5 GB") |
| · weights on the GPU | ~13,000 to 13,560 | 13,560 MiB CUDA0 model buffer at 8K with the default margin (`docs/mitsuba-prompt-helper-study.md`, `-lv 4`); at 64K `--fit` moves ~464 MiB more expert weight to RAM than at 32K (`docs/bonsai-2-study.md`), so 64K is lower: **estimate** |
| · KV cache, 64K, q8_0 | ~680 | **estimate**, computed: 10 full-attention layers × K and V × 2 KV heads × head dim 256 × 34 bytes per 32 values (q8_0) = 10.6 KiB per token, × 65,536. Assumes the Qwen3.5-family hybrid layout (30 of 40 layers are linear attention with a fixed small state); the GGUF metadata would confirm it. Consistent with the measured 32K→64K displacement above (KV plus compute buffers) |
| · compute buffers and CUDA context | ~400 to 700 | **estimate**: the remainder |
| · expert layers left in system RAM | 17 to 18 of 40 | measured with `-lv 4` at 32K/64K (`docs/bonsai-2-study.md`) |
| · one expert layer | ~525 (measured moves: 760 for one layer, 780 for two) | **estimate**: 22 GB file / 40 layers (`docs/INSTALL.md` gives 22 GB). The measured moves (`docs/bonsai-2-study.md`, `docs/mitsuba-prompt-helper-study.md`) disagree with each other because `--fit` can split a layer; use 0.5 to 0.8 GiB per layer |
| Free with the model loaded | ~1,024 | the default `--fit-target`; Windows measured 1,664 at 64K (16,376 − 14,712), where the margin plus rounding was larger |
| Plex, one 1080p H.264 → H.264 transcode | 600 (range 150 to 560 reported) | **estimate**: forum reports of ~350 MB and ~560 MB per 1080p transcode and a 150 to 200 MB Plex process in `nvidia-smi` ([Plex forum](https://forums.plex.tv/t/hardware-requirements-suggestions-around-dedicated-gpu-nvidia-quadro-p2000/453114/12), [Plex forum](https://forums.plex.tv/t/plex-hardware-transcoding-hdr-nvenc/689111)) |
| Plex, one 1080p HEVC (10-bit) → H.264 transcode | 700 (range 300 to 700) | **estimate**: as above, plus larger 10-bit decode surfaces. No HEVC-specific figure was found |
| Plex, one 4K HEVC HDR → 1080p with tone mapping (for scale; not in the budget) | 1,400 (range 600 to 1,400) | **estimate**: forum reports of 1.2 to 1.4 GB per 4K transcode ([Plex forum](https://forums.plex.tv/t/plex-hardware-transcoding-hdr-nvenc/689111)) and one measured 600 MB on a GTX 1660 Ti ([ZimaSpace blog](https://shop.zimaspace.com/blogs/tech-ai-hub/plex-gpu-memory-remote-4k-streaming)) |
| Sunshine, one stream (NVENC encode plus capture and colour conversion) | 500 (range 300 to 600) | **estimate**: an idle NVENC session at 2880×1800 holds ~130 MB ([NVIDIA forum](https://forums.developer.nvidia.com/t/reducing-nvenc-memory-usage/165325)), plus a CUDA context and capture buffers. Not measured for Sunshine |
| Safety margin | 256 | choice: fragmentation and driver growth |

NVENC's concurrent-session cap on GeForce cards (8 since early 2024,
[Plex forum](https://forums.plex.tv/t/nvidia-increases-transcode-limits-from-5-to-8/867879)) is far above anything
here, so memory, not sessions, is the limit.

**Today, against one Plex stream plus Sunshine:** about 1,024 free with the model loaded, against 700 + 500 + 256 =
1,456 needed for HEVC (1,356 for H.264). It is **~430 MiB short** (~330 for H.264), which matches #823's "they do not
fit". Without the safety margin it is still 76 to 176 short.

## Options

All VRAM figures are on the 16,376 MiB card with the desktop at 600 MiB. Headroom is what stays free with the model
loaded plus **one 1080p HEVC Plex transcode (700) and one Sunshine stream (500)**; the H.264 case is 100 MiB better.
Tokens per second are **not measured** for any option (see "Measuring later"). The decode column is an estimate from a
simple model: today's 85 tok/s with ~18 expert layers on the CPU, a fixed GPU part of ~4 ms per token and ~0.43 ms
per CPU expert layer, scaled by layer size for other quants.

| Option | Change | Model VRAM | Headroom with 1 Plex + Sunshine | Decode tok/s | Quality | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 0. Today | Q4_K_XL, 64K, default margin | ~14,750 | **−176** (doesn't fit) | 85 (measured, #823) | baseline | |
| **A. Smaller quant** | same model at UD-Q3_K_XL (~17 GB, **estimate** scaled from the Q4 file; not checked that this exact quant is published), 64K, `--fit-target 2048` | ~13,730 | **+848** | not measured; estimate 95 to 130 (about 11 expert layers on the CPU instead of 18, each ~25% smaller; the simple model gives ~130) | lower: a 3-bit quant; tool-call reliability unknown. Must pass the canary gates (`docs/canary-evals.md`) as #170/#174 did | the quant alone, with the default margin, would leave only 1,024 free and fail like today: `--fit` fills the card again |
| **B. Partial offload, margin** | Q4_K_XL unchanged, 64K, `--fit-target 2048` | ~13,730 | **+848** | not measured; estimate 76 to 81 (two more expert layers on the CPU, ~1 GiB) | unchanged: same weights, same context | one flag; reversible; RAM: ~1 GiB more of the model in system RAM (the load gate's `model_ram_gb: 14` already covers it) |
| B2. Partial offload, fixed layers | Q4_K_XL, 64K, `--fit off --n-gpu-layers 99 --n-cpu-moe 20` (experts of 20 of 40 layers in RAM, fixed) | ~13,700 (**estimate**: today's ~14,750 with ~18 layers in RAM, minus 2 × ~525) | **+876** (estimate) | as B: not measured; estimate 76 to 81 | unchanged | the footprint no longer depends on what is free at load: it doesn't shrink when a stream is already running, and the load fails if less than ~13.7 GB is free. Needs its own `-lv 4` check of the real size |
| B3. Smaller context only | Q4_K_XL, **32K**, default margin | ~14,750 | **−176** (doesn't fit, same as today) | not measured; 32K decoded ~2% faster than 64K on Windows (`docs/phase0-results.md`) | worse for long files: 64K was needed for the memory-library tasks (`docs/phase0-results.md`, follow-up table) | frees nothing under `--fit`: the margin stays 1,024 and `--fit` moves expert weights back onto the GPU. Measured: 14,806 MiB at 32K vs 14,712 at 64K (`docs/phase0-results.md`). With B2's fixed placement, 32K would add the ~340 MiB of KV it saves (**estimate**) to B2's headroom |
| C. Fits entirely | an IQ2-class quant (~11 GB, **estimate**), 64K, all layers on the GPU | ~12,300 | **+2,276** | not measured; likely above 100 | much lower: 2-bit | no RAM offload at all; not recommended for agent work without a full canary run |

Combinations follow the same arithmetic: each extra 1,024 of `--fit-target` buys one more ~700 MiB Plex stream and
costs about two expert layers (estimate about −6 tok/s per 1,024 with Q4).

## Recommendation

**Option B: keep Qwen3.6-35B-A3B Q4_K_XL at 64K and add `--fit-target 2048`.** It is the only option that fits one
transcode plus Sunshine with no quality change, it is a single argument on the llama-server unit, and rolling it back
is removing that argument. It is preferred over B2 because `--fit` adapts to what is free at load, while a fixed
placement can fail to load beside a running stream. Option A is the next step only if B's measured decode lands below ~70 tok/s, and only
after the canary gates pass on the 3-bit quant.

### Guard thresholds

The model then coexists with one hardware transcode and a desktop stream, so the guard stops treating every
transcode as a reason to park.

| Trigger | Today (`gpu_guard`) | Proposed |
| --- | --- | --- |
| Game (library folder, Big Picture window) | park | park (unchanged). On Linux `process_paths()` and `window_titles()` return nothing, so this trigger is off there; follow-up 5 |
| Sunshine desktop stream | no action | no action (unchanged); it is inside the 2,048 margin |
| Plex hardware transcodes | park on the first | new `gpu_guard.plex_transcodes_allowed` (default 0 = today's behaviour; the tower sets 1). Park when the count of hardware transcodes is above it, or when any of them has a 4K or HDR tone-mapped source (~1,400, over budget). Needs #811's Plex detection on Linux |
| Free VRAM | none | new `gpu_guard.min_free_vram_mib` (tower: 256; 0 = off). Park when `nvidia-smi` free VRAM stays below it for two polls. This is the backstop for every estimate above being wrong: a second stream, a 4K source, an unknown CUDA app. The reading already exists (`harness_modules/local_model/resources.py`, `gpu_reading`) |

How this ties to #811: #811 makes `Detector._plex()` work on Linux (a token file instead of the registry). This design
needs a **count** and a **source resolution** per hardware transcode from it, not only "any". `hw_transcodes()` already
returns one entry per session, so the count is free; the resolution may need `/status/sessions` (the media's
`videoResolution`) beside `/transcode/sessions`, to be checked in #811. Without a Plex token on Linux, `nvidia-smi
--query-compute-apps=pid,process_name,used_memory` lists `Plex Transcoder` processes with their VRAM, which gives the
count (not the resolution) with no credential (follow-up 4; to verify that NVENC-only transcodes appear there).

Settings that stay: `poll_seconds` 10, `resume_after_seconds` 180, `lazy_load` true, `drain_timeout_seconds` 300 for
the Plex-count trigger. The free-VRAM trigger gets a shorter drain (follow-up 3): by the time it fires, something is
already short of memory.

### Page-out and page-in when a transcode starts

1. **A first transcode (within `plex_transcodes_allowed`).** Nothing happens. It takes its ~700 MiB from the 2,048
   margin; the model keeps serving. This is the change from today.
2. **A transcode over the allowance, or a 4K/HDR one, or free VRAM under the floor for two polls (≤ 20 s).**
   Page-out as today: the queue pauses, the model turn in flight finishes (up to `drain_timeout_seconds`, or the
   shorter free-VRAM drain), then `ModelWarmer.park` writes the pause flag and stops llama-server. ~13.7 GB comes free
   once the container exits. **On Linux this needs follow-up 9 first:** `ServerControl.stop()` finds the server with
   `netstat -ano` and stops it with `taskkill`, both Windows-only, and `install/run-server.sh` checks the pause flag
   only before it launches a container. Today a park on Ubuntu writes the flag but leaves the running container
   holding its VRAM. Whether the hold of 2026-10-23 stopped the container some other way (by hand, or through the
   unit) was not checked. A second stream that started before the park may already have fallen back to software
   transcoding; Plex picks the GPU again for the next session, not the running one.
3. **Page-in.** Once the triggers have been gone for `resume_after_seconds` (180 s), the queue reopens. With
   `lazy_load` the model stays parked until a turn, an endpoint request or *Load local model now* needs it, then loads
   in ~30 s (#823). `--fit` measures free VRAM at that moment: if the one allowed transcode is still running, the
   model loads ~700 MiB smaller and keeps 2,048 free above it. It stays that size until its next idle unload
   (`--sleep-idle-seconds 600`), which is fine.
4. **A load while the allowed transcode runs** is not blocked: one transcode is no longer a trigger.

The manual hold, the RAM gate (`min_available_ram_gb`, `model_ram_gb`) and *Unload now* don't change.

Not chosen: reloading with a bigger margin (say 4,096) instead of parking when a second stream starts. It keeps the
model available under load but costs a ~30 s reload each way plus code to switch arguments, for a case (two
transcodes at once) that the owner hasn't reported. Revisit if it happens often.

## Measuring later

Tokens per second, the Plex and Sunshine costs, and the Ubuntu desktop figure are **not measured**. The GPU hold is
on and #823 doesn't authorise a measurement, so none was taken. When the owner lifts the hold and says on a card that
a measurement run may use the GPU:

0. **Stop production and check the card is free.** Until follow-up 9 lands, *Unload now* and the guard's park don't
   stop the Linux container (see "Page-out and page-in"), so the owner stops it through its unit instead:
   `systemctl --user stop agent-harness-main-llama.service`. Its `ExecStart` is `run-server.sh`, whose `TERM` trap runs
   `docker stop` on the container. Then confirm `docker ps --filter name=agent-harness-main-llama` lists nothing and
   `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` shows no llama-server before going on.
   Every "model parked" below means this state. Afterwards `systemctl --user start agent-harness-main-llama.service`
   brings it back; the supervisor waits while the pause flag exists.
1. **Baseline, model parked.** `nvidia-smi --query-gpu=memory.used,memory.total,encoder.stats.sessionCount
   --format=csv` with the desktop idle. That gives the Ubuntu desktop figure and the real card total.
2. **Plex, model parked.** The owner starts one 1080p H.264 and then one 1080p HEVC hardware transcode from a client.
   Read `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` and the GPU line once each
   stream is steady (about 30 s in). Optionally one 4K HDR source for the guard's 4K rule.
3. **Sunshine, model parked.** One desktop stream at the owner's usual resolution and frame rate; same two queries.
4. **Option B.** Production stays stopped (step 0): two copies don't fit, and a leftover copy would push the
   throwaway server's experts to the CPU. Then a throwaway server on
   another port, with the image and model file named in the tower's `agent-harness-main-llama.service` and the same
   arguments `run-server.sh` passes, plus the margin and verbose fit logging:

   ```
   docker run --rm --name ah823-measure --gpus all --network host -v <model.gguf>:/models/model.gguf:ro <image> \
       -m /models/model.gguf --host 127.0.0.1 --port 8097 --ctx-size 65536 --flash-attn on --parallel 1 --jinja \
       --metrics --fit on --cache-type-k q8_0 --cache-type-v q8_0 --fit-target 2048 -lv 4
   ```

   Once `/health` answers, record the split from the log (layers, expert layers overflowing, CUDA0 and CUDA_Host
   buffers), `nvidia-smi --query-gpu=memory.used` and `--query-compute-apps`, and the server's resident memory
   (`docker stats --no-stream ah823-measure`). Decode and prompt speed: `POST http://127.0.0.1:8097/completion` with
   fixed prompts of about 2K, 16K and 58K tokens (`n_predict: 200`, `cache_prompt: false`), reading
   `timings.predicted_per_second` and `timings.prompt_per_second` from each answer. Then `docker stop ah823-measure`
   and repeat without `--fit-target 2048` as the baseline, in the same session. `bakeoff/context_test.py` doesn't do
   this on Ubuntu as it stands: it starts its own server from `bakeoff/models.yaml` (Windows paths) and reads process
   memory through PowerShell (follow-up 8).
5. **Coexistence.** With option B loaded and idle, repeat 2 and 3 together, and confirm `nvidia-smi` free memory stays
   above 256 and that Plex's dashboard shows "(hw)" for the stream.
6. **Option A, only if B is too slow.** Steps 4 and 5 with the Q3_K_XL file (and B2's arguments if a fixed
   footprint is wanted), then the canary gates
   (`docs/canary-evals.md`).

Write the results into this doc and replace each **estimate** it supersedes.

## Follow-up issues (proposed, not filed)

No code changes in #823. These are the code and config changes this design implies:

1. **llama-server margin.** Add `--fit-target 2048` to the Qwen arguments in `install/install.sh` (behind an installer
   flag such as `--vram-headroom-mib`, so a machine without Plex keeps the default), and to the tower's
   `agent-harness-main-llama.service`. Decide whether `ops/llama-server/run-qwen.ps1` follows.
2. **`gpu_guard.plex_transcodes_allowed`** (default 0, range 0 to 4), with the 4K/HDR rule. Registry entry in
   `harness_modules/local_model/settings.py`, docs in `docs/resource-guard.md`. Depends on #811.
3. **`gpu_guard.min_free_vram_mib`** (default 0 = off) as a trigger from `gpu_reading`, two consecutive polls, with its
   own drain timeout (for example `vram_drain_timeout_seconds`, default 30).
4. **Plex detection without a token on Linux** (feeds #811): count `Plex Transcoder` processes in `nvidia-smi
   --query-compute-apps`, and show per-process VRAM in Actions → Resources diagnostics.
5. **Game and Sunshine detection on Linux.** `process_paths()` and `window_titles()` are Windows-only (#811 asks to
   document this); a `/proc` scan for the game library folders, and a Linux way to tell a Sunshine Big Picture
   session from a desktop stream.
6. **`doctor` warnings.** WARN when `gpu_guard` is on and no Plex detection source works on this platform (with #811);
   WARN when `plex_transcodes_allowed > 0` but the llama-server arguments have no `--fit-target` of at least about
   `700 × allowed + 500 + 256` MiB; WARN when `min_free_vram_mib` is not below the `--fit-target`, which would park
   right after every load.
7. **Metrics.** Export free VRAM and `encoder.stats.sessionCount` as `harness_resource_*` series for the
   `nvidia-gpu` dashboard.
8. **Measurement run** (owner-gated, "Measuring later" above), then update this doc. Optionally port
   `bakeoff/context_test.py` first: a `--base-url` mode that measures an already running server, and Linux process
   memory from `/proc` instead of PowerShell, so later model studies on the tower can reuse it.
9. **Linux page-out (prerequisite for the guard on the tower).** Make `ServerControl.stop()` work on Linux: stop the
   `agent-harness-<instance>-llama` container (`docker stop`, as `run-server.sh`'s own trap does), or have
   `run-server.sh` watch the pause flag while the container runs and stop it when the flag appears. Replace the
   `netstat -ano` lookup with psutil or `ss`. Without this, none of the guard changes above free VRAM on Ubuntu.
   *Done in #835:* `run-server.sh` watches the flag and stops its container; `docs/resource-guard.md` describes it.
