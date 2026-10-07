# Mitsuba-ComfyUI-27B as an Images prompt-writer and image describer (issue #307)

Research only. The protocol below was committed before any run and is unchanged. Results are below it: the
**re-run of 2026-10-06 (verdict: no-go)** and the first attempt, which stopped at the RAM floor.

**Verdict: no-go for a new model.** Qwen's rewrite (b) was preferred on 6 of 10 prompts and Mitsuba's (c) on 1; 3 were
ties. Decision 6 therefore gives **"use Qwen with a system prompt; no new model"** for "Improve prompt". Mitsuba's
hand-over cost (11.5 s) is under the 15 s limit, so speed is not the reason. Its prompt writing is: it echoed the
rough prompt twice, looped once, and missed the 60-120 word rule 3 times. Mitsuba's image descriptions were good, but
"Describe image" alone is not in decision 6 and is left for the owner (see "Verdict against decision 6").

## Protocol (frozen before the run)

- Machine: the tower, GPU hold on, always-on Qwen on 8090 paused by the hold. Candidate and the comparison Qwen are served
  on the bakeoff port **8081** only. No change to `config/harness.yaml`, the always-on server, installers or
  `harness_modules/images/`.
- Context size: 8192 for every server (the task needs well under 2K tokens). RAM is watched continuously; the run stops
  and deletes the candidate if free RAM drops below 2 GB (the failure that stopped #170).
- Images: ComfyUI through its normal HTTP API (`/prompt`, `/history`, `/view`) with the repo's `flux-fast` graph
  (`harness_modules.images.service.workflow`, imported read-only), `standard` 1024x1024, **seed 307**, installed models only.
- Variants per prompt: (a) raw prompt; (b) current local Qwen (Qwen3.6-35B-A3B UD-Q4_K_XL, the bakeoff profile) with the
  system message below, thinking off; (c) Mitsuba v1.18 `PQ2_0` with the same system message, thinking off.
- Describe image: 5 of the generated images captioned by Mitsuba + mmproj; quality notes only (Qwen has no vision).
- Go criterion (fixed): go only if (c) is preferred over (b) on at least 6 of 10 prompts **and** it adds under 15 s to a
  generation on the GPU path without an extra swap. If (b) is preferred on 5 or more: "use Qwen with a system prompt; no
  new model". The agent records its own preference; the owner visual check can overturn it.

### System message (identical for (b) and (c))

```
You rewrite rough image ideas into one detailed text-to-image prompt for a FLUX model. Output only the final prompt as a
single paragraph, 60 to 120 words. Keep every subject, every piece of quoted text (verbatim, in quotes) and every
layout instruction from the user. Describe subject, setting, lighting, camera or art style and colour. Do not add
explanations, headings, lists or negative prompts.
```

### The 10 synthetic prompts

| # | Rough prompt | Kind |
| --- | --- | --- |
| 1 | a lighthouse on a cliff at dusk | plain |
| 2 | corgi astronaut floating in a kitchen | plain |
| 3 | a bakery shop front with a sign that says "Hot Bread" | text |
| 4 | poster for a jazz night, big title "BLUE HOUR", small line "Fridays 8pm" | text |
| 5 | a coffee mug with the words "Monday Again" printed on it, on a wooden desk | text |
| 6 | three red apples in a row on a white table, left one largest, right one smallest | layout |
| 7 | split image: left half a snowy forest, right half a desert at noon | layout |
| 8 | an old woman knitting by a window in the rain | plain |
| 9 | futuristic night market in a flooded city, neon reflections | plain |
| 10 | a watercolor fox sleeping in autumn leaves | plain |

(3 text-in-image: #3, #4, #5. 2 strict layout: #6, #7. No owner prompts or private data.)

## Re-run: no-go (2026-10-06 21:49-22:00)

The run used the tower (RTX 4070 Ti Super 16 GB, 32 GB RAM). The GPU hold was taken at 21:48:57 with
`harness gpu pause --duration-seconds 10800`, and production Qwen (8090) stayed parked. Docker Desktop and WSL were
stopped for the night, leaving 20.2 GB available. A separate 0.5 s watchdog would have killed anything listening on 8081
or 8191 below 2 GB available, on top of the driver's own 2 GB guard. **It never fired.** The lowest reading was
8.74 GB, during the CPU-offload phase. Every phase of `ops/mitsuba_study.py` ran to completion. Raw output is in the
gitignored `runs/mitsuba-307/` (also `C:/AI/downloads/mitsuba-study/run/`): the 30 PNGs, `write-*.json`,
`describe.json`, `handover*.json`, `render-times.json` and the server logs. Docker was not needed for any step.

### Files and binary (same pins as the first attempt; re-verified)

The download from revision `1cebf3503275fa2b0419cce39783bcf3f3926f86` hashed to the same SHA-256 values as the first
attempt:

| File | SHA-256 |
| --- | --- |
| `Mitsuba-ComfyUI-27B-v1.18-PQ2_0.gguf` (7,319,078,304 bytes) | `cc45a52123860da817b704e3b35495e90c9a67c74ae697a6314775a4c7318791` |
| `mmproj-Q8_0.gguf` (629,246,976 bytes) | `6807ede61d570bb86ba34b756a0fa109edc33668604de867c6ea6d8f1d631903` |
| `llama-prism-b10754-2459f68-bin-win-cuda-13.3-x64.zip` | `4a589d87d69dc102843c3cdfe6619a6fbbbd731c8cb52f4ab9da82a6f2dfdd1a` |
| `cudart-llama-bin-win-cuda-13.3-x64.zip` | `1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e` |

The fork reported `version: 0.2.0-dev (build 10754, commit 2459f68b5)` and ran without the silent exit that
`KNOWN_ISSUES` describes for CUDA 13.3 Windows builds. Nothing was built, so there are no build flags. The optional
`HiMitsuba-Uncensored-LoRA.gguf`, added to the repo on 2026-10-04, was not downloaded or used.

### Exact server flags

Every server ran on `127.0.0.1:8081` with `--flash-attn on --parallel 1 --jinja -c 8192 --temperature 0.6 --top-k 20
--top-p 0.95`. Each request sent `chat_template_kwargs: {enable_thinking: false}`.

- **(b) Qwen**, stock `C:/AI/llama.cpp/b10950/llama-server.exe`, `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`:
  `--fit on --fit-target 256 --cache-type-k q8_0 --cache-type-v q8_0 --load-mode none`.
- **(c) Mitsuba**, fork: `--mmproj mmproj-Q8_0.gguf --no-mmproj-offload --reasoning off --cache-type-k q4_0
  --cache-type-v q4_0 --load-mode none --n-gpu-layers 99` (CPU offload: `--n-gpu-layers 0`). These are the card's
  flags except the context (8192) and the load mode.

**Option (a) for the baseline.** The owner chose to spill fewer expert layers to system RAM rather than use a smaller
setting. Context, quant and KV type are the same as in the first attempt. Only `--fit`'s VRAM margin changes:
`--fit-target 256` replaces the default 1024 MiB, as in #170 (PR #421). `--load-mode none` comes from #405 (PR #420).
Both splits below were read with `-lv 4` at 8K:

| `--fit` at ctx 8192 | layers on GPU | expert layers overflowing to RAM | CUDA0 model buffer | CPU (CUDA_Host) expert buffer | VRAM at `/health` |
| --- | --- | --- | --- | --- | --- |
| default margin (1024 MiB) | 41 | 17 | 13,560 MiB | 7,754 MiB | 14,919 MiB |
| **`--fit-target 256` (used)** | 41 | **15** | 14,341 MiB | **6,973 MiB** | 15,703 MiB |

With option (a), two fewer expert layers go to RAM, and about 780 MiB of weights moves from RAM to VRAM. Together with
`--load-mode none` (no mmap page-in), Qwen kept 11.2 GB or more available for the whole run. The first attempt
fell to 1.90 GB after two prompts.

### Prompt writing: (b) Qwen vs (c) Mitsuba

| Prompt | (b) words | (c) words | Preferred | Why |
| --- | --- | --- | --- | --- |
| 1 lighthouse | 126 | 81 | **c** | (c)'s image is a clean dusk sunset. (b)'s has a stray rainbow-like beam. (b) also ran over 120 words |
| 2 corgi astronaut | 103 | 75 | **b** | (b) shows zero gravity (floating utensils, spilled milk). (c) reads as a dog standing in a suit |
| 3 "Hot Bread" | 112 | **55** | **b** | Both signs are spelled right. (b) is a full shop front with an awning and windows. (c) is under 60 words, and its "bread stacks" become odd loaf piles |
| 4 "BLUE HOUR" poster | 111 | 73 | **b** | Both texts are right. (b) is a clean, readable poster. (c) put a stadium crowd under the title |
| 5 "Monday Again" mug | 99 | 97 | **b** | (c)'s text **loops** ("The wooden desk is a medium-sized, round object, made of wood." repeated). FLUX still drew a mug, but the prompt is unusable |
| 6 three apples, sizes | 108 | **23** | tie | (c) **echoed the rough prompt**. All three images are almost identical and correct |
| 7 split forest/desert | 99 | 78 | tie | Both keep the split. (c) adds a divider line. (a) is equally good |
| 8 woman knitting, rain | 108 | 64 | **b** | (b) has visible rain and a moody lamp-lit room. (c) is a pastel interior with little rain |
| 9 flooded night market | 101 | 64 | **b** | Similar images. (b) is a bit more cinematic. (c) mostly restates the input ("The scene is set in a night") |
| 10 watercolor fox | 100 | **7** | tie | (c) **echoed the rough prompt** word for word. The images are nearly identical |

**Count: (b) 6, (c) 1, tie 3.** Mitsuba missed the 60-120 word rule on 3 of 10 prompts (55, 23 and 7 words) and
looped on 1. Qwen ran over once (126 words). Every variant, including the raw prompt (a), spelled "Hot Bread",
"BLUE HOUR", "Fridays 8pm" and "Monday Again" correctly. Only (a)'s secondary door text in #3 was garbled. FLUX.2
klein already follows short prompts well, so a rewrite mostly changes style, not correctness. The card's "strict
conditions" claim (6/10 vs 3/10) was not borne out with this system message.

Committed examples (`docs/assets/mitsuba-307-examples.jpg`, prompts 3, 4, 5 and 8; columns a / b / c):

![Prompts 3, 4, 5 and 8: raw, Qwen and Mitsuba renders](assets/mitsuba-307-examples.jpg)

All 30 images are in `runs/mitsuba-307/images/` (`pNNa|b|c.png`).

### Speed and memory

| | (b) Qwen GPU | (c) Mitsuba GPU | (c) Mitsuba CPU (`-ngl 0`) |
| --- | --- | --- | --- |
| Load to `/health` | 14.7 s | 5.6 s | 7.6 s |
| VRAM after load | 15,701 MiB | 7,903 MiB (mmproj on CPU) | 1,069 MiB |
| Available RAM after load / after 10 prompts / lowest | 12.87 / 11.21 / 11.15 GB | 18.78 / 15.96 / 15.95 GB | 12.32 / 8.88 / 8.74 GB |
| Decode | 76.9-78.7 tok/s | 61.4-64.7 tok/s | 3.9-4.4 tok/s |
| Time to first token | 0.20-0.66 s | 0.17-0.52 s | 2.1-3.0 s |
| Time per prompt | 1.8-2.6 s | 0.3-2.3 s | 4.2-29.1 s |

FLUX `flux-fast` (FLUX.2 klein 4B fp8, 4 steps, 1024x1024, seed 307) took 2.1-2.2 s per image when warm and 6.2 s for
the first. ComfyUI ran as a separate process on 8191, used only through `/prompt`, `/history`, `/view` and `/free`,
with its output, temp and input folders under the run directory. Image defaults and installed models were not
changed. 8189, the port the first attempt planned, is taken by `tailscaled` on the tower.

### Hand-over cost (decision 5)

GPU path (`handover.json`): Qwen unload 0.6 s, then FLUX cold 3.7 s. Next, Mitsuba load, one prompt and unload took
10.0 s (load 5.6 s, prompt 2.1 s). FLUX cold again in a fresh ComfyUI took 5.2 s. **Mitsuba adds 11.5 s** to a
generation, under the 15 s limit. It needs one extra load and unload.

CPU-offload path (`handover-cpu.json`): FLUX stays resident (13.1 GB VRAM) while Mitsuba on the CPU loads and writes
one prompt. That took 29.1 s (7.6 s load, 19.5 s for 75 tokens at 4.4 tok/s). A warm FLUX render with Mitsuba still
resident took 2.6 s, against 2.1 s without it. Available RAM went down to 9.97 GB. This avoids the swap but adds
about 20-30 s per prompt, which is too slow.

For comparison, the Qwen path needs no swap. Production Qwen is already loaded when the owner presses "Improve
prompt". It writes the prompt in about 2 s at 77 tok/s, and the image job then unloads it, as every Images job does
today.

### Describe image (Mitsuba + mmproj; quality notes only)

| Image | Notes |
| --- | --- |
| 3a "Hot Bread" | Correct: brick front, red-on-white sign, "Hot Bread" read exactly, bread inside, the door sign. It **invented** door text ("Irish Bread", "Bread in the Toast") from glyphs that FLUX had garbled |
| 4a "BLUE HOUR" | Correct: both lines of text read exactly, colours and layout right. It took a dark shape in the corner for a saxophone |
| 6a apples | Exact: three apples, largest left to smallest right, stems, white surface, "no visible text" |
| 7a split | Detailed and right: snowy conifers on the left, dunes with footprints on the right, snowy mountains in the distance |
| 9a night market | Right overall: wet reflective street, stalls, people. It said the Chinese signs are "not fully readable" instead of inventing them |

Each caption took 10.6-12.1 s. 9.4-9.6 s of that is the time to first token, because the card's `--no-mmproj-offload`
encodes the image on the CPU. Decode ran at 64 tok/s. VRAM was 7.9 GB, and 17.1 GB RAM stayed available. Captions
were good: all quoted text was read exactly, and the only error was text invented from garbled glyphs.

### Verdict against decision 6

- (c) was preferred over (b) on **1 of 10** prompts, short of the required 6, so the go criterion fails.
- (b) was preferred on **6 of 10**, which is 5 or more, so the recommendation is **"use Qwen with a system prompt; no new
  model"**.
- The speed half would have passed (11.5 s < 15 s), but it does not matter.

**No follow-up issue for Mitsuba.** A possible follow-up, for the owner to decide: an Images "Improve prompt" button
that sends the system message above to the always-on Qwen with thinking off. It adds about 2 s and no new model.
"Describe image" needs a vision model, and Qwen3.6 in the harness has none. Mitsuba's captions were good, but adding
it only for captions costs a 7.3 GB second model, a second llama.cpp binary (`run-qwen.ps1`, the installers and
`harness/doctor.py` all assume one stock build) and about 11 s per swap. #170 reached the same conclusion about
the fork for Bonsai 2. This study does not decide that trade; the owner can open a separate issue if captions matter.

The owner visual check (last acceptance box) is still open. The 30 images are in `runs/mitsuba-307/images/`.

### License and provenance

- Repo: `isichan-ai/Mitsuba_and_HiMitsuba-27B-GGUF`, renamed on 2026-10-04 from `Mitsuba-ComfyUI-27B-GGUF`. The card
  says the model files are unchanged, and the hashes above confirm it for PQ2_0 and the mmproj.
- License: Apache-2.0 (card front matter, `docs/LICENSE`). Base model: `Qwen/Qwen3.8-27B`, Apache-2.0 (Alibaba Cloud),
  per `docs/NOTICE`. The card calls it a "self-made ternarization of the official Qwen3.8-27B weights (not derived from
  Bonsai's weights)", stored in Prism ML's PQ2_0 / PTQ1_0 formats (Apache-2.0).
- The mmproj is "taken unchanged" from `OS-Software/Ternary-Bonsai-2-27B-Uncensored-Heretic-GGUF` (Apache-2.0).
  It has the same SHA-256 as Prism ML's own Bonsai 2 mmproj recorded in #170 (`6807ede6…1903`).
- The card does not disclose the tuning data or method for the ComfyUI prompt skill. The fork is MIT, like upstream
  llama.cpp.

### Cleanup

- The model, mmproj, both fork zips and the extracted fork were deleted at 22:02. The candidate did not win. Only the
  run outputs remain (`C:/AI/downloads/mitsuba-study/run/`, 109 MB, and the copy in the gitignored `runs/mitsuba-307/`).
- The GPU hold was released at 22:00:18 with `harness gpu resume`. The guard was `clear` at 22:00:23, with Qwen parked
  and unloaded, as it was before the hold. `harness models warm` then loaded production Qwen, which was `loaded`/`ready`
  at 22:01:23 with 7.2 GB available.
- The harness daemons were not stopped or restarted. No file changed in `config/`, the always-on server, the
  installers or `harness_modules/images/` (`workflow()` was imported read-only).

### Driver changes for this run (`ops/mitsuba_study.py`)

- ComfyUI moved from port 8189 to 8191.
- Qwen gets `--fit-target 256 --load-mode none`, and Mitsuba gets `--load-mode none`.
- The `handover` phase now also times the Qwen unload.
- A new `handover-cpu` phase was added.
- The CPU hand-over renders different prompts. The first try reused one prompt and got ComfyUI's cached result in
  0.5 s, so that measurement was discarded and the phase re-run.

## First attempt: stopped at the RAM floor, no verdict (2026-10-06)

The run was **aborted cleanly by the 2 GB free-RAM guard during variant (b)**, before the candidate was ever loaded.
The candidate was not the cause: the guard fired while serving the *current local Qwen* (Qwen3.6-35B-A3B
UD-Q4_K_XL, `--fit on`, q8_0 KV, context 8192, the bakeoff profile) on the otherwise idle tower (free RAM 20.8 GB before
the launch). Prompts 1 and 2 were written at about 71 and 73 tokens/s, then available RAM fell to 1.90 GB and the driver
killed the server. Free RAM returned to 20.8 GB and VRAM to 545 MiB right after; no daemon was touched, no image was
rendered, and no file in `config/`, the always-on server, installers or `harness_modules/images/` changed.

So the go criterion (decision 6) is **not evaluated**: no (b) vs (c) preference, no hand-over numbers, no image captions.

### What was established

| Item | Evidence |
| --- | --- |
| Model | `isichan-ai/Mitsuba_and_HiMitsuba-27B-GGUF` (renamed 2026-10-04 from `Mitsuba-ComfyUI-27B-GGUF`), revision `1cebf3503275fa2b0419cce39783bcf3f3926f86` |
| Quant file | `Mitsuba-ComfyUI-27B-v1.18-PQ2_0.gguf`, 7,319,078,304 bytes, SHA-256 `cc45a521…c7318791` (full: `cc45a52123860da817b704e3b35495e90c9a67c74ae697a6314775a4c7318791`); equals the Hugging Face LFS etag |
| Vision encoder | `mmproj-Q8_0.gguf`, 629,246,976 bytes, SHA-256 `6807ede61d570bb86ba34b756a0fa109edc33668604de867c6ea6d8f1d631903` (card: taken unchanged from OS-Software/Ternary-Bonsai-2-27B-Uncensored-Heretic-GGUF) |
| License | Apache-2.0 in the card front matter and `docs/LICENSE` (Apache 2.0 text); `docs/NOTICE` states the weights are a modified Qwen3.8-27B (Apache-2.0, Alibaba Cloud), formats and mmproj by Prism ML (Apache-2.0) |
| Fork | Not built: PrismML publishes Windows CUDA binaries. `PrismML-Eng/llama.cpp` release tag `prism-b10754-2459f68`, commit `2459f68b5c0eb26261fd5a81682004b93cd645ba`, asset `llama-prism-b10754-2459f68-bin-win-cuda-13.3-x64.zip` (178,666,352 bytes, SHA-256 `4a589d87d69dc102843c3cdfe6619a6fbbbd731c8cb52f4ab9da82a6f2dfdd1a`) plus `cudart-llama-bin-win-cuda-13.3-x64.zip` (SHA-256 `1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e`). It reported `version: 0.2.0-dev (build 10754, commit 2459f68b5)`. Driver here is CUDA UMD 13.4. No #170 build existed to reuse |
| Card run flags | `--mmproj mmproj-Q8_0.gguf --no-mmproj-offload --reasoning off --jinja --temperature 0.6 --top-k 20 --top-p 0.95 --ctx-size 131072 --cache-type-k q4_0 --cache-type-v q4_0 --n-gpu-layers 99` (this study planned context 8192) |
| Card claims (unverified here) | strict-format prompts 6/10 vs 3/10 base; coding 4/100; reading 48/100; vision 87.8; ~119 tokens/s on an RTX 5090; thinking must be off |
| Disk | All downloaded files (model, mmproj, both zips, extracted fork) were deleted after the abort |

### Observation worth acting on

The comparison baseline itself does not fit in this machine's RAM guard at 8K context. If the Images "Improve prompt"
feature is pursued with the current Qwen, that is a finding on its own: serving the 22 GB Qwen3.6 with expert layers
spilled to RAM on a 16 GB card drove free RAM below 2 GB. A fair re-run should serve (b) with fewer spilled layers or
accept a manual floor change, and ideally run when the machine is otherwise quiet (as #170's re-run).

The re-run above followed that advice: option (a) and a quiet machine. It also used `--load-mode none` (#405).
