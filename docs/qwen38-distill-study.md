# Qwen3.8-35B-A3B-Distill (empero-ai) vs Qwen3.6-35B-A3B (#174)

Run 2026-10-05/06 on the RTX 4070 Ti Super 16 GB / 32 GB RAM tower, GPU hold on, always-on Qwen (port 8090) stopped
(it was already paused by the hold for the whole run), bake-off server on port 8081, llama.cpp **b10950**. Qwen was
re-run in the same session with the same build and sampling; phase0 numbers are not used. Raw output lives in the
gitignored `runs/` (`20261005-231328` hard, `20261006-000527` core, `context-20261005-230451` Qwen and
`context-20261005-230933` candidate).

## Verdict: **no-go (provenance not established)**

Every performance gate passes, but decision 6 says provenance not established means no-go, and it is not established
(see "License and provenance"). The owner can overrule this from the evidence below; the numbers say the model would
be a fair drop-in for Qwen3.6 if the provenance is accepted. No follow-up implementation issue is proposed.

## Candidate

| | |
| --- | --- |
| Repo | `empero-ai/Qwen3.8-35B-A3B-Distill-GGUF` (base `empero-ai/Qwen3.8-35B-A3B-Distill`) |
| Revision | `b1f9d1dcc3de8aa867669b0ab919384aeeb9b8d5` (last modified 2026-09-16) |
| File | `Qwen3.8-35B-A3B-Q4_K_M.gguf`, 21,713,462,944 bytes (Qwen's UD-Q4_K_XL is 22.4 GB) |
| SHA-256 | `196103269085bc54c9b8f49ed21e9f53e1b56b465e8b796c6d8e31e06f63cfa5` (computed locally, matches the model page) |
| Architecture | Qwen3.6-35B-A3B (30 Gated DeltaNet + 10 full-attention layers, 256 experts, 8 routed). Loads and serves on b10950, so no llama.cpp upgrade is needed |
| Settings | `--fit on --cache-type-k q8_0 --cache-type-v q8_0`, sampling temperature 0.6, top_p 0.95, top_k 20, min_p 0 (same as Qwen; the model card recommends the same) |

Entry added to `bakeoff/models.yaml` as `qwen3.8-35b-a3b-distill` (its path points at a file that was deleted after the study).

## Commands (identical for both models)

```
python -m bakeoff.context_test --model <m> --ctx 4096,32768,65536
python -m bakeoff.run --suite hard --repeats 3 --models qwen3.6-35b-a3b,qwen3.8-35b-a3b-distill
python -m bakeoff.run --suite core --repeats 3 --models qwen3.6-35b-a3b,qwen3.8-35b-a3b-distill
```

Note: the `core` suite currently holds 12 tasks, not the 5 named in the issue (the 5 are all included and all 3/3 for
both models). Throughput per run is the median over agent turns as recorded by `run.py`.

## Gates

| Gate | Qwen3.6 | Candidate | Result |
| --- | --- | --- | --- |
| Hard-suite passes (10 tasks x 3), need >= Qwen - 3 = 25 | 28/30 | 27/30 | **pass** |
| Per repeat (min / median / max) | 9 / 9 / 10 | 9 / 9 / 9 | reported |
| Core-suite passes (12 tasks x 3) | 39/39 | 39/39 | tie |
| Invalid tool calls, hard + core (need <= Qwen) | 0 + 0 | 0 + 0 | **pass** |
| Decode speed >= 90% at each perf point | see below | | **pass** (lowest 90.8%) |
| Memory at ctx 32768, `vram_mib` + `server_private_mib` after longest prompt (need <= Qwen) | 14756 + 15748 = 30504 MiB | 14723 + 15721 = 30444 MiB | **pass** (-0.2%, no gain for the companion 20% rule) |

The hard suite is the only place the two differ: both models fail `sqlite_report` (Qwen 1/3, candidate 0/3); every
other task is 3/3 for both. With ~10 tasks the one-task margin is noisy, so this is a tie, not evidence that either
model is better. Tool errors: Qwen 3 (hard) + 6 (core), candidate 1 + 1. Avg turns 10.8 vs 10.5 (hard), 5.6 vs 5.4
(core). Median gen tok/s in agent runs 75.1 vs 79.2 (hard), 76.1 vs 80.2 (core).

### Decode speed per perf point (tok/s, candidate / Qwen)

| ctx-size | prompt tokens | Qwen | Candidate | Ratio |
| --- | --- | --- | --- | --- |
| 32768 | 1939 | 65.4 | 72.3 | 111% |
| 32768 | 15539 | 69.8 | 64.6 | 93% |
| 65536 | 1939 | 68.9 | 67.5 | 98% |
| 65536 | 15539 | 69.6 | 63.2 | 91% |
| 65536 | 29139 | 67.9 | 65.3 | 96% |
| 65536 | 58277 | 59.7 | 63.6 | 107% |

Perf points are `< ctx-3000`, so 32K measures only 2K and 16K (30K is not below 29768); 4K measures none. The 16K
points are close to the 90% line and single measurements, so treat the pass as marginal. Nothing missed, so no spill
explanation was needed.

### Memory (MiB; all fields)

| ctx | model | load s | after_load vram / private / ws / avail | after_longest vram / private / ws / avail |
| --- | --- | --- | --- | --- |
| 4096 | Qwen | 14.7 | 14703 / 15424 / 14351 / 765 | same |
| 4096 | cand | 13.5 | 14637 / 15376 / 14695 / 5765 | same |
| 32768 | Qwen | 40.9 | 14732 / 15488 / 14573 / 1132 | 14756 / 15748 / 20897 / 737 |
| 32768 | cand | 10.7 | 14695 / 15465 / 14471 / 5765 | 14723 / 15721 / 20433 / 490 |
| 65536 | Qwen | 36.9 | 14639 / 15428 / 14200 / 7215 | 14661 / 15694 / 19575 / 661 |
| 65536 | cand | 24.7 | 14636 / 15338 / 14123 / 6730 | 14658 / 15695 / 19108 / 1532 |

No ctx failed to load for either model. Available RAM fell under 1 GB at the longest prompts for both models (and
reached 490 MiB for the candidate); `system_avail_mib` before load varies with whatever else the machine was doing, so
only the VRAM and server-private columns are used for the gate.

## Other observations

- **GPU-guard reload time:** not measured through the guard (the harness daemons were not touched, and the hold kept
  the guard paused). The bake-off load time (`load_seconds`, cached file) is 10.7 to 24.7 s for the candidate and
  12.6 to 12.8 s in the suites, comparable to Qwen's 12.7 s.
- **Vision:** the card says the fine-tune is text-only, vision inherited and not evaluated; an `mmproj-Qwen3.8-35B-A3B-F16.gguf`
  (899 MB) is published. Not tested here.
- **Installer/doctor:** same architecture and quant family on the same llama.cpp build, so no change would be needed
  to run it; nothing was changed.
- Vendor benchmark claims (ARC-Challenge 0.548 to 0.591, MMLU flat 0.834) are unverified and were not used.

## License and provenance (for the owner's call)

- Publisher: empero-ai on Hugging Face (first MoE distillation by this publisher).
- License: Apache-2.0, stated as inherited from the Qwen3.6-35B-A3B base (Alibaba Qwen).
- Teachers: "Qwen3.8 2.4T A95B and Qwen3.8 Flash Next". The card gives no license or terms for the teachers, and does not
  say how the traces were obtained.
- Training data: "curated teacher traces from our internal Qwen3.8 distillation datasets"; no public dataset, and part of
  the prompts came from community endpoint interactions, unspecified.
- Method: off-policy SFT on teacher traces, updating attention and expert stacks.
- Consequence: whether the teacher outputs may be used to train a derivative under Apache-2.0 is not documented, so
  provenance is not established. By decision 6 this is a no-go unless the owner decides otherwise.

## Cleanup

The candidate directory was deleted after the study (it did not win), the bake-off server was stopped, the GPU hold was
left as found, and the always-on Qwen stays paused by the hold and reloads on demand when the hold is lifted.
