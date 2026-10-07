# Bonsai 2 27B (PrismML) vs Qwen3.6-35B-A3B (#170)

Run 2026-10-06 21:19-21:33 on the RTX 4070 Ti Super 16 GB / 32 GB RAM tower. The GPU hold was on, production Qwen
(port 8090) was parked by the hold, and Docker Desktop and WSL were stopped for the night, leaving about 20.7 GB
available. The bake-off server used port 8081. Qwen was re-run in the same session on the stock llama.cpp **b10950**
build. The candidate ran on the PrismML fork. A 0.5 s watchdog would have stopped the 8081 server below 2 GB
available. It never fired: the lowest reading was 11,669 MiB. Raw output is in the gitignored `runs/`:
`context-20261006-212222` (Qwen), `context-20261006-212509` (candidate), and the extra runs
`context-20261006-212744` (f16 KV) and `context-20261006-212944` (PTQ1_0).

This is the second attempt. The first, on the same night, stopped on low RAM before any candidate measurement
(see the issue comments).

## Verdict: **no-go (decode speed gate fails at every perf point)**

Bonsai 2 decodes at **71-82% of Qwen's speed** at all six perf points. The fixed gate is 90%. The gate applies to both
the replacement and the companion role, so neither role passes. Two extra runs were made to check that the miss is
not caused by a setting: the vendor's preferred packing for Ada cards (PTQ1_0) and an f16 KV cache. They reach at
most 85%. The hard-suite and 5-task core gates could not run tonight (they need Docker; see "Not run").
Passing them cannot rescue the verdict, so no re-run is proposed. No follow-up implementation issue is proposed.

The memory result is large: VRAM + server private memory is **51% lower** at 32K (VRAM alone is 43% lower), and the
model loads in 2.6-4.6 s instead of 14.6 s. If the decode gate is ever revisited, for example if the fork's faster
PTQ1_0 CUDA kernel (fork PR #218, "fix in review") lands, this is the number that makes Bonsai interesting as a
companion. Re-running would then need a Docker night for the hard and core suites.

## Candidate

| | |
| --- | --- |
| Repo | `prism-ml/Ternary-Bonsai-2-27B-gguf` (base model `Qwen/Qwen3.8-27B`) |
| Revision | `b072e1d3b35a0a630cece372c2127528e0994386` (last modified 2026-09-25) |
| File (gated runs) | `Ternary-Bonsai-2-27B-PQ2_0.gguf`, 7,206,168,928 bytes |
| SHA-256 | `3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1` (computed locally; matches the HF LFS hash) |
| Supplementary file | `Ternary-Bonsai-2-27B-PTQ1_0.gguf`, 5,946,648,928 bytes, SHA-256 `53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3` |
| Vision file | `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf`, 629,246,976 bytes, SHA-256 `6807ede61d570bb86ba34b756a0fa109edc33668604de867c6ea6d8f1d631903` |
| Architecture | Qwen3.8-27B hybrid attention (about 75% linear), 64 blocks, dense. Ternary g128 weights in a Hadamard-rotated basis |
| Fits | Entirely in VRAM (no expert spill): 7.9 GB at 4K, 9.0 GB at 32K, 10.2 GB at 64K |

PQ2_0 is the packing the demo's `setup.ps1` downloads, and it was the packing pinned by the first attempt. The model
card says PTQ1_0 decodes faster on Ada cards, so PTQ1_0 was also measured (below). It did not change the verdict.

### Fork binary

Stock llama.cpp rejects `PQ2_0`/`PTQ1_0` as unknown types (model card). The fork is used as a prebuilt release. It was
not built locally, so there are no local build flags.

| | |
| --- | --- |
| Release | `prism-b10743-adfffbe` in `PrismML-Eng/llama.cpp` (published 2026-09-25), pinned by `Bonsai-demo` `setup.ps1` at commit `330450a1a120bbfb2dd3b0f0bbc4315a7dbc8120` |
| Commit | `adfffbe41b2cabcd51fff326ab045662265062bb` (`llama-server --version`: build 10743, commit adfffbe41, MSVC 19.44.35229.0) |
| Assets | `llama-prism-b10743-adfffbe-bin-win-cuda-12.4-x64.zip` SHA-256 `1b849f713bee42fda258de83770cd422e8f48dd631ce370eb0641f6458c69d87`; `cudart-llama-bin-win-cuda-12.4-x64.zip` SHA-256 `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` (both match the release digests) |
| Why CUDA 12.4 | The fork's KNOWN_ISSUES says CUDA 13.3 Windows builds can exit silently after the banner, and recommends 12.4 on Windows |

The fork's own build docs say a source build needs `-DGGML_CUDA=ON`. It may also need `-DCMAKE_CUDA_ARCHITECTURES=89`
for the 40-series and `-DGGML_CUDA_FA_ALL_QUANTS=ON` for quantized KV with flash attention. The prebuilt release ran
q8_0 KV with `--flash-attn on` without errors.

## Settings and commands

As decision 8 says, `bakeoff/models.yaml` was edited locally and the edit was not committed. It was restored with
`git checkout` afterwards. The top-level `llama_server` stayed on b10950 for Qwen. For the candidate runs it pointed at
the fork's `llama-server.exe`. Common args were unchanged (`--host 127.0.0.1 --flash-attn on --parallel 1 --metrics
--jinja`), on port 8081.

```
  qwen3.6-35b-a3b:   # owner's option (a): spill fewer expert layers; --load-mode none per #405
    path: C:/AI/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
    args: [--fit, "on", --fit-target, "256", --cache-type-k, q8_0, --cache-type-v, q8_0, --load-mode, none]
    sampling: {temperature: 0.6, top_p: 0.95, top_k: 20, min_p: 0.0}

  bonsai-2-27b:
    path: C:/AI/models/bonsai2/Ternary-Bonsai-2-27B-PQ2_0.gguf
    args: [--fit, "on", --cache-type-k, q8_0, --cache-type-v, q8_0, --load-mode, none]
    sampling: {temperature: 1.0, top_p: 0.95, top_k: 20, min_p: 0.05}   # model card, thinking mode
```

```
python -m bakeoff.context_test --model qwen3.6-35b-a3b --ctx 4096,32768,65536   # stock b10950
python -m bakeoff.context_test --model bonsai-2-27b --ctx 4096,32768,65536      # llama_server -> fork
```

**Option (a) for the Qwen baseline.** The owner chose to spill fewer expert layers to system RAM rather than use a
smaller setting. The context, quant and KV type stay the same as in #174. Only `--fit`'s VRAM safety margin changes:
`--fit-target 256` replaces the default 1024 MiB. With `-lv 4`, `--fit` reported these splits:

| ctx | default margin (1024 MiB) | `--fit-target 256` (used) |
| --- | --- | --- |
| 32768 | 41 layers, 17 overflowing, 13,988 MiB VRAM, CPU expert buffer 8,047 MiB | 41 layers, **16** overflowing, 14,744 MiB, CPU buffer **7,290 MiB** |
| 65536 | 41 layers, 18 overflowing, 13,896 MiB VRAM, CPU expert buffer 8,511 MiB | 41 layers, **17** overflowing, 14,652 MiB, CPU buffer **7,754 MiB** |

That is one fewer expert layer spilled to RAM and about 760 MiB less CPU-side weight memory at each context. The cost
is about 760 MiB more VRAM. `--load-mode none` (#405, `docs/resource-guard.md`, PR #420) was used for both servers.
Qwen then loaded in about 14.6 s with no mmap page-in, and available RAM never fell below 11.7 GB. #174 used the
default mmap load, so its RAM dipped under 1 GB at long prompts.

The protocol and commands are the same as #174 (`docs/qwen38-distill-study.md`). The Qwen flags differ from #174 in
`--fit-target 256` and `--load-mode none`, so the absolute Qwen numbers are not directly comparable with that study's.
Within this study both models used the same session, commands and load mode.

## Gates (decision 3, fixed before the run)

| Gate | Qwen3.6 | Candidate | Result |
| --- | --- | --- | --- |
| Decode >= 90% of Qwen at each perf point | see below | 70.8-82.1% | **fail** (6 of 6 points below 90%) |
| Hard-suite passes >= Qwen - 3 (of 30) | not run | not run | **not evaluated** (needs Docker) |
| Tool-call parse failures <= Qwen (hard + core) | not run | not run | **not evaluated** (needs Docker); smoke test 10/10 vs 10/10, below |
| Replacement: no more VRAM (32K, after longest prompt) | 15,687 MiB | 8,983 MiB | pass (moot) |
| Companion: VRAM + server private at 32K cut >= 20% | 15,687 + 23,772 = 39,459 MiB | 8,983 + 10,358 = 19,341 MiB | pass, **-51.0%** (moot) |
| Companion alternative: beat Qwen on pass rate | not run | not run | not evaluated |

Min/median/max over 3 repeats applies to the suite gates, which did not run. `context_test` takes one measurement per
perf point, as in #174.

### Decode speed per perf point (tok/s, 200 generated tokens, `cache_prompt: false`)

| ctx-size | prompt tokens | Qwen | Bonsai PQ2_0 | Ratio | PTQ1_0 (extra) | f16 KV (extra) |
| --- | --- | --- | --- | --- | --- | --- |
| 32768 | 1939 | 78.6 | 62.0 | 78.9% | 64.7 (82.3%) | |
| 32768 | 15539 | 74.9 | 56.6 | 75.6% | 58.6 (78.2%) | |
| 65536 | 1939 | 75.8 | 62.2 | 82.1% | 64.7 (85.4%) | 62.8 (82.8%) |
| 65536 | 15539 | 72.6 | 56.6 | 78.0% | 58.5 (80.6%) | 57.4 (79.1%) |
| 65536 | 29139 | 69.0 | 52.0 | 75.4% | 53.6 (77.7%) | 53.2 (77.1%) |
| 65536 | 58277 | 62.3 | 44.1 | 70.8% | 45.1 (72.4%) | 45.3 (72.7%) |

The perf points are the sizes `< ctx - 3000`, so the 32K run measures only 2K and 16K and the 4K run measures none.
These are the same points as #174. The gap is consistent with a dense model compared with an A3B MoE. Bonsai reads
about 7 GB of ternary weights per token. Qwen reads only about 3B active parameters, even with 16-17 expert layers on
the CPU. The model card's own RTX 4090 figure (tg128 81.2 tok/s for PQ2_0) scales to roughly 54 tok/s on this card's
672 GB/s, against 62 measured here. That means the kernels are not underperforming on this machine.

Prompt processing is faster for Bonsai up to 30K (1,591 vs 1,144 tok/s at 2K, 1,694 vs 1,493 at 16K) and slower at 58K
(1,266 vs 1,412). This is not a gate.

### Memory (MiB; all `context_test` fields)

| ctx | model | load s | after_load vram / private / ws / avail | after_longest vram / private / ws / avail |
| --- | --- | --- | --- | --- |
| 4096 | Qwen | 14.6 | 15655 / 23153 / 7473 / 13725 | same |
| 4096 | Bonsai | 4.6 | 7905 / 8806 / 949 / 19847 | 7905 / 8806 / 949 / 20151 |
| 32768 | Qwen | 14.6 | 15669 / 23509 / 7820 / 13337 | 15687 / 23772 / 8128 / 13160 |
| 32768 | Bonsai | 2.6 | 8969 / 9925 / 978 / 20125 | 8983 / 10358 / 1503 / 19525 |
| 65536 | Qwen | 14.7 | 15571 / 23915 / 8320 / 12957 | 15601 / 24185 / 8634 / 12360 |
| 65536 | Bonsai | 2.6 | 10217 / 11243 / 1013 / 19953 | 10229 / 11648 / 1544 / 19579 |
| 32768 | PTQ1_0 | 2.6 | 7825 / 8713 / 921 / 19560 | 7839 / 9154 / 1446 / 19391 |
| 65536 | PTQ1_0 | 2.5 | 9073 / 10031 / 954 / 19889 | 9085 / 10445 / 1486 / 19398 |
| 65536 | f16 KV | 2.5 | 11925 / 12985 / 1024 / 18994 | 11937 / 13363 / 1550 / 18969 |

No context failed to load for either model. With `--load-mode none`, Qwen's private bytes include the whole read-in
file (about 23.5 GB, of which only about 8 GB is resident). The private-based gate metric therefore looks larger for
Qwen than it did in #174, which used mmap (15.7 GB private). The VRAM column alone gives the same answer: -43% at 32K.
The working set gives an even larger one: 1.5 GB vs 8.1 GB. Bonsai's private memory roughly tracks its VRAM, because
the CUDA host mirror is counted as private memory.

## Not run (Docker unavailable)

`bakeoff/run.py` runs every task in a Docker sandbox (`bakeoff/sandbox.py`), and Docker Desktop was stopped for the
night. These commands were **not** run:

```
python -m bakeoff.run --suite hard --repeats 3 --models qwen3.6-35b-a3b,bonsai-2-27b
python -m bakeoff.run --suite core --repeats 3 --models qwen3.6-35b-a3b,bonsai-2-27b
```

As a Docker-free stand-in, a first-turn tool-call smoke test was run instead. It is an observation and does not
replace the gate. The script sends the bake-off agent's `SYSTEM_PROMPT` and `TOOLS`, `tool_choice: auto`,
`max_tokens: 8192` and each model's sampling. It uses 5 prompts (list files, run pytest, read README, write and run a
file, find TODOs) and runs each twice. Result: **Bonsai 10/10** responses ended in `finish_reason: tool_calls` with
arguments that parse as JSON objects, in 0.7-2.6 s. **Qwen 10/10**, in 0.9-2.0 s. Both picked the same tools.
The fork's KNOWN_ISSUES still lists malformed or looping tool calls in agent loops as open. A single first turn
cannot show that.

## Other observations

- **GPU-guard reload time:** not measured through the guard, because the harness daemons were not touched and the
  hold kept the guard paused. The bake-off load time to `/health` with `--load-mode none` from a warm file cache is
  2.5-4.6 s for Bonsai and 14.6-14.7 s for Qwen.
- **Vision:** works on the fork with `--mmproj Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` (load 4 s, 10.7 GB VRAM at 32K).
  A synthetic 640x360 PNG with a red rectangle, a blue circle and the text "Invoice 4821 - total $317.50" was
  described correctly: both shapes and colours, and the text transcribed exactly. This took 2 s and 124 tokens with
  `reasoning_effort: medium`. Qwen3.6 in the harness has no vision. Vision is an observation only, not a gate.
- **Reasoning:** the template's default effort is `xhigh`, and `high` returns HTTP 500 (KNOWN_ISSUES). Harness
  integration would need `reasoning_effort: medium` and a large `max_tokens`.
- **Installer/doctor:** not exercised. Running Bonsai would need a second llama.cpp binary, because the stock build
  rejects the file types. `run-qwen.ps1`, the installers and `harness/doctor.py` all assume one stock build, so a
  per-model binary path would be new work in each of them. Nothing was changed.
- Vendor benchmark claims (98.2% of FP16 over 14 thinking-mode benchmarks, BFCL v3 74.92) are unverified and were not
  used.

## License and provenance (for the owner's call)

- Publisher: Prism ML, Inc. (`prism-ml` on Hugging Face, `PrismML-Eng` on GitHub). `NOTICE.txt`: "copyright 2026-present
  Prism ML, Inc. It is available under the Apache 2.0 license" and attribution is requested ("Created using Bonsai by
  Prism ML").
- License: Apache-2.0 (model card, `LICENSE`, and the HF `license` tag). The demo repo is Apache-2.0 and the
  llama.cpp fork is MIT, like upstream.
- Base model: `Qwen/Qwen3.8-27B`, Copyright 2026 Alibaba Cloud, Apache-2.0 (stated in `NOTICE.txt`). The architecture
  is unchanged.
- Method: ternary g128 weights with FP16 group scales in a blockwise Hadamard-rotated basis (model card and whitepaper
  `bonsai-2-27b-whitepaper.pdf` in Bonsai-demo). The card names no teacher model.
- Training or calibration data: **not disclosed**. Neither the model card nor the 14-page whitepaper says what data
  produced the ternary weights. The whitepaper's only data references are its evaluation sets. Unlike #174, there is no
  third-party teacher whose terms are in question. The open question is only whether undisclosed
  calibration/training data is acceptable for an Apache-2.0 derivative of an Apache-2.0 base.
- This does not affect the verdict, which is no-go on decode speed.

## Cleanup

- The candidate (PQ2_0, PTQ1_0, mmproj), the fork build and its zips were deleted after the study. Only the stock
  b10950 build and the original three GGUFs remain in `C:/AI`.
- `bakeoff/models.yaml` was restored with `git checkout`, so no candidate entry is committed. To re-run, add back the
  `bonsai-2-27b` entry above and point `llama_server` at the fork.
- The watchdog was stopped (lowest available 11,669 MiB, never fired).
- The GPU hold was released with `harness gpu resume` at 21:33. The harness daemons were never stopped or restarted. With `lazy_load` the guard
  leaves production Qwen parked and reloads it on the first request (`warmup.ModelWarmer.ensure_loaded`). Before the
  hold it was also unloaded (sleeping), so this matches its earlier state.
