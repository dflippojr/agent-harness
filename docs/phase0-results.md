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

## Hard suite (2026-09-14)

`bakeoff/tasks_hard.py`: 10 tasks × 2 repeats, 50 turns / 30 min each, mostly graded by hidden tests. Same baseline
loop and model profiles as above. Raw output: `runs/20260914-102032/` (regraded with `bakeoff.rescore` after two
checker fixes, see below).

| Model | Pass | Avg turns | Invalid tool calls | Tool errors | Avg wall s | Gen tok/s | Prompt tok/s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Qwen3.6-35B-A3B** | **19/20** | 10.1 | 0 | 1 | 47 | 74 | 416 |
| gpt-oss-20b | 16/20 | 13.9 | 1 | 10 | 26 | 165 | 2412 |

| Task | Qwen3.6 | gpt-oss |
| --- | --- | --- |
| multi_bug_inventory | 2/2 | 2/2 |
| duration_parser | 2/2 | 2/2 |
| merge_conflict | 2/2 | 1/2 |
| log_correlation | 2/2 | 2/2 |
| sqlite_report | 1/2 | 0/2 |
| perf_fix | 2/2 | 2/2 |
| compose_diagnosis | 2/2 | 2/2 |
| flaky_shared_state | 2/2 | 2/2 |
| cli_json_output | 2/2 | 1/2 |
| trace_config_flow | 2/2 | 2/2 |

Every failure was checked by hand and is a real model error:

- `sqlite_report` (3 of 4 misses): two runs joined refunds to orders and then summed order totals, counting an order
  once per refund; one used `BETWEEN '2026-04-01' AND '2026-06-30'`, which drops orders placed later on June 30.
- `merge_conflict` (gpt-oss): merged, then made a follow-up commit that also omits port 80 for https, contradicting
  the spec.
- `cli_json_output` (gpt-oss): its own new tests call the CLI with `--file` after the subcommand and fail.

Checker fixes found during review (both regraded):

- `flaky_shared_state` had a hidden test requiring the caller's `items` dict not be mutated. The prompt never asked
  for that, and Qwen's fix of the stated root cause was correct, so the test was removed (Qwen 0/2 → 2/2).
- `merge_conflict` required HEAD itself to be the merge commit; it now accepts follow-up commits after a real merge.
  No verdict changed.

Takeaway: the hard suite separates the models (gpt-oss loses 4, Qwen 1), but Qwen is still close to the ceiling.
Qwen's typical hard task takes ~10 turns and under a minute.

## D1 reference: OpenHands CLI 1.16.0 (2026-09-14)

Same hard suite, models and llama-server profiles, driven by `bakeoff/reference.py --harness openhands` in a
no-internet container. Raw output: `runs/openhands-20260914-105238/` (killed once by Claude Code's low-memory
watchdog after 8 Qwen runs, then resumed detached).

| Harness + model | Pass | Avg wall s | Median wall s |
| --- | --- | --- | --- |
| baseline `agent.py` + Qwen3.6 | **19/20** | 47 | 37 |
| OpenHands + Qwen3.6 | 18/20 | 126 | 108 |
| baseline `agent.py` + gpt-oss-20b | **16/20** | 26 | 12 |
| OpenHands + gpt-oss-20b | 14/20 | 136 | 115 |

- OpenHands is not better with either model, and is 2.7× (Qwen) to 5× (gpt-oss) slower per task. Some early Qwen
  runs happened while the machine was paging, so part of that gap may be memory pressure, but the later runs are
  just as slow.
- OpenHands + Qwen misses are real model errors (`"1hm"` accepted by the duration parser; a wrong SQLite filter).
- Several OpenHands + gpt-oss misses come from how the harness copes with the model rather than from reasoning:
  - When gpt-oss emits a tool call llama-server can't parse (HTTP 500, "does not match the expected peg-native
    format"), OpenHands retried at least 4 times and then quit with an empty answer. The baseline resamples at the
    profile's temperature 1.0 and carries on. OpenHands probably sends its own lower temperature (not confirmed).
  - One run repeated the same file view 7 times and was stopped, apparently by OpenHands' stuck detection.
  - OpenHands' much larger prompts pushed some requests past the 32K context.
## D1 reference: OpenCode 1.18.30 (2026-09-14)

`bakeoff/reference.py --harness opencode`, same setup (web tools and ask-the-user denied). Raw output:
`runs/opencode-20260914-130013/` (run detached; the Qwen half ran with available RAM as low as 239 MiB).

| Harness + model | Pass | Avg turns | Avg wall s |
| --- | --- | --- | --- |
| baseline `agent.py` + Qwen3.6 | **19/20** | 10.1 | 47 |
| OpenCode + Qwen3.6 | 18/20 (19/20 counting the compaction case below) | 8.1 | 81 |
| OpenHands + Qwen3.6 | 18/20 | 13.5 | 126 |
| baseline `agent.py` + gpt-oss-20b | **16/20** | 13.9 | 26 |
| OpenCode + gpt-oss-20b | **16/20** | 13.0 | 25 |
| OpenHands + gpt-oss-20b | 14/20 | 19.3 | 136 |

- OpenCode matches the baseline's accuracy and, with gpt-oss, its speed. With Qwen it is ~1.7× slower despite fewer
  turns: its ~7K-token system prompt is costly for Qwen's slow prompt processing (gpt-oss processes prompts ~6× faster).
- Misses:
  - `sqlite_report` (3): the same refund double-count / `BETWEEN` mistakes as the other harnesses.
  - `cli_json_output` (gpt-oss): its tests fail.
  - `log_correlation` (both models): context compaction. Qwen stated the correct answer (u0271, 233, MemoryError),
    then OpenCode compacted twice at the 32K limit and injected "Continue if you have next steps…"; Qwen's reply
    ("The investigation is complete…") became the final message. gpt-oss overflowed the context (39.9K tokens),
    and after compaction answered about "media attachments". Long-running tasks at 32K context need compaction that
    preserves the answer; this matters for Phase 1 too.

## D1 decision input

- Neither existing harness is "dramatically better" than the minimal baseline on these tasks and local models.
  OpenHands is worse and much slower; OpenCode is roughly equal.
- The model dominates: every harness fails `sqlite_report` the same way.
- Prompt size matters on this hardware: Qwen's prompt processing (~400–900 tok/s) makes heavy system prompts slow.
- OpenCode remains a credible building block (client/server, sessions, compaction, mobile/desktop clients) if the
  daemon wraps it rather than reimplementing the agent loop. That choice is separate from the D1 performance check.

## Memory finding (blocks always-on inference)

Every llama-server process commits roughly the model's size in system memory even when the weights live in VRAM
(12.5–14.7 GB private bytes; unchanged with `--load-mode none` or `dio`, so it is not mmap). This looks like WDDM
charging GPU allocations against commit. With ~22 GB already committed at idle (WSL/Docker/apps) and an
auto-managed page file of only ~3 GB, total commit reached 36.7/37.8 GB and Claude Code's low-memory watchdog killed
the bake-off twice. Physical RAM was not exhausted. Fix candidate: a larger fixed page file on the C: SSD
(admin + reboot, user decision).
