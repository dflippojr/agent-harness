# Mitsuba-ComfyUI-27B as an Images prompt-writer and image describer (issue #307)

Research only. Status: **protocol committed before any run** (this section is frozen; results are appended below it).

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

## Result: stopped at the RAM floor, no verdict (2026-10-06)

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

### Re-run

`ops/mitsuba_study.py` is the committed driver (phases `write-qwen`, `write-mitsuba`, `write-mitsuba-cpu`,
`images`, `describe`, `handover`; separate ComfyUI on port 8189 through its HTTP API; 2 GB RAM guard; seed 307). It was
exercised only up to the first two Qwen prompts; the other phases are untested. The prompts and protocol above are
unchanged and still valid for a re-run. The owner visual check is not applicable yet.
