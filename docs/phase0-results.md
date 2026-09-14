# Phase 0 results (2026-09-13)

Hardware: `dflippotower`, RTX 4070 Ti Super 16 GB, 32 GB RAM. Runtime: llama.cpp b10950 (CUDA 13.3), 32K context,
one slot. Harness: `bakeoff/agent.py` baseline loop, 13 tasks × 2 repeats, one Docker sandbox per run.
Raw output: `runs/20260913-224648/` (gpt-oss, Qwen) and `runs/20260913-230430/` (partial).

## Agent tasks

| Model | Pass | Avg turns | Invalid tool calls | Avg wall s | Gen tok/s (agent turns) | Peak VRAM MiB |
| --- | --- | --- | --- | --- | --- | --- |
| **Qwen3.6-35B-A3B** UD-Q4_K_XL, `--fit on` (experts partly in RAM) | **26/26** | 5.6 | 0 | 21 | 72 | 15127 |
| gpt-oss-20b MXFP4, all on GPU | 23/26 | 9.0 | 1 | 9 | 162 | 12973 |
| Devstral-Small-2 24B IQ4_XS, `--fit on` | 6/6 before the run was killed (see memory below) | — | — | 5–169 | — | 14667 |

gpt-oss misses were genuine model errors: in both `rename_across_files` runs it left `calc_total` in the test file
despite the prompt saying "and the tests", and one `recover_from_error` run produced a wrong manifest.

## Throughput (single request, `cache_prompt: false`)

| Context tokens | Qwen3.6 prompt s | Qwen3.6 gen tok/s | gpt-oss prompt s | gpt-oss gen tok/s | Devstral prompt s | Devstral gen tok/s |
| --- | --- | --- | --- | --- | --- | --- |
| ~1.9K | 11.4* | 66 | 0.4 | 161 | 1.5 | 26 |
| ~7.8K | 10.3 | 70 | 1.1 | 154 | 4.8 | 23 |
| ~15.5K | 17.9 | 69 | 2.2 | 149 | 9.8 | 19 |
| ~29K | 33.1 | 67 | 4.5 | 137 | 20.5 | 15 |

\* First request after load includes warm-up.
Devstral all-on-GPU (`-ngl 99`) was faster (34–44 tok/s gen, ~2,000 tok/s prompt) but that configuration pushed the
machine past its commit limit.

## Decision

- **Default model: Qwen3.6-35B-A3B.** Perfect score, fewest turns, zero invalid tool calls, and steady ~70 tok/s
  decode regardless of context. Its cost is prompt processing (~880 tok/s): a cold 29K-token context takes ~33 s,
  so the Phase 1 daemon should lean on llama-server's prompt cache (keep one session's prefix warm) and compaction.
- **Fast/secondary model: gpt-oss-20b.** 2× faster decode and ~8× faster prompt processing, 23/26. Good for
  summaries, compaction, titles, and cheap subtasks, but less reliable at following every instruction.
- **Dropped: Devstral-Small-2 24B.** Dense 24B doesn't fit 16 GB with 32K context without spilling, and it is the
  slowest of the three once it does.
- **Caveat:** Qwen hit the ceiling of this task set, so it can't distinguish "good" from "great". Harder, longer
  tasks are needed before comparing against an existing harness (D1).

## Memory finding (blocks always-on inference)

Every llama-server process commits roughly the model's size in system memory even when the weights live in VRAM
(12.5–14.7 GB private bytes; unchanged with `--load-mode none` or `dio`, so it is not mmap). This looks like WDDM
charging GPU allocations against commit. With ~22 GB already committed at idle (WSL/Docker/apps) and an
auto-managed page file of only ~3 GB, total commit reached 36.7/37.8 GB and Claude Code's low-memory watchdog killed
the bake-off twice. Physical RAM was not exhausted. Fix candidate: a larger fixed page file on the C: SSD
(admin + reboot, user decision).
