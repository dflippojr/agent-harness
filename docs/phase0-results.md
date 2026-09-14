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

**D1 decided (user, 2026-09-14): build our own agent loop.** OpenCode's only visible gain was offset by
compaction/context failures, and sessions and clients are designed explicitly in our plan anyway.

## Context size test (2026-09-14)

`bakeoff/context_test.py`, Qwen3.6 with `--fit on`, q8_0 KV cache. Raw output: `runs/context-20260914-145812/`.

| ctx | VRAM MiB | Server private MiB | Gen tok/s @ ~2K prompt | Gen tok/s @ 15.5K | Longest prompt: tokens / prompt s / gen tok/s |
| --- | --- | --- | --- | --- | --- |
| 32K | 14806 | 15485 | 58.1 | 68.4 | 15.5K / 19.6 s / 68.4 (earlier run: 29K / 33 s / 67) |
| **64K** | 14712 | 15427 | 57.0 | 67.3 | 58.3K / 68.9 s / 59.6 |
| 128K | 14697 | 15478 | 50.5 | 60.2 | 116.6K / 151.1 s / 48.4 |

- Memory is flat: `--fit` keeps VRAM full and moves slightly more expert weight to RAM as the KV cache grows.
- 64K costs ~2% decode speed versus 32K; 128K costs ~12%. Prompt processing stays ~770–850 tok/s.
- **64K is close to free** and doubles the room for project configs and long files. A cold 58K prompt takes ~70 s,
  so keep stable prefixes (system prompt, project config) cached.
- The server's working set grows from ~14.6 GB to ~19.6 GB while it processes a long prompt (expert pages touched),
  which is what drives available RAM to ~500 MiB during bake-offs.

**GPU "SW Power Cap" (2026-09-14):** the Grafana GPU board showed this reason during bake-offs at 70–80% utilization,
<140 W and <60 °C. 100 ms nvidia-smi sampling during a Qwen request: the limit is 285 W (default; range 100–305 W);
the flag was active in 4 of 257 samples, all during one transient where instantaneous draw hit 185 W while the 1 s
average read 84 W during a P-state step. Total capping time rose ~145 ms for the whole request; HW slowdown, thermal
slowdown and power braking counters are all zero. Split CPU/GPU MoE inference is bursty (median 22% utilization,
29 W), so brief spikes trip the driver's power-management flag. It is harmless and unrelated to voltage.

## Memory-library suite (2026-09-14)

`bakeoff/tasks_memory.py`: 5 tasks on a synthetic library mirroring agent-memory-library (CLAUDE.md → AGENTS.md →
index → category → capsules; a ~16K-token project memory file; a 2K-character single-line index summary; an inbox note
with incidental medical and financial details). Baseline loop, 32K context, 2 repeats. Raw output:
`runs/20260914-150953/` (regraded after two checker fixes).

| Task | Qwen3.6 | gpt-oss-20b |
| --- | --- | --- |
| memlib_newest_entry (newer dated entry deep in the 16K-token file wins) | **0/2** | 2/2 |
| memlib_capsule_detail (capsule beats category bullet) | 2/2 | 2/2 |
| memlib_add_note (dated note, uncertainty kept, no other files) | 2/2 | 2/2 |
| memlib_index_edit (change one sentence in a long one-line paragraph) | 2/2 | 1/2 |
| memlib_promote_inbox (promote project facts, leave sensitive details out) | **0/2** | **0/2** |
| **Total** | 6/10 | 7/10 |

- `newest_entry`: Qwen paged through the file with `read_file` (400 lines per call), found the March "Zigbee" entry at
  line 401 and answered without reaching the August "switched to Thread" entry at line 1,291. gpt-oss searched for
  "Garden sensor" first, saw both, and read the newer one. The failure is the reading strategy, not context size:
  `read_file`'s 400-line page limit is independent of `--ctx-size`.
- `promote_inbox`: two runs copied the celiac diagnosis into health memory; one (Qwen) left out the diagnosis and
  account number but still added a savings note to finance memory, beyond "project context"; one gpt-oss run died on
  malformed tool-call JSON.
- `index_edit` (gpt-oss): dropped the capsule pointer from the edited sentence.
- Checker fixes (regraded): the uncertainty check now accepts any hedging wording ("still figuring out whether…"
  was wrongly rejected, 3 runs); the index check no longer rejects mentioning "moved from planning" (1 run).

Follow-up on `newest_entry` (2026-09-14), Qwen only, 3 repeats each:

| Setup | Big file (~16K tokens) | Small file (~6K tokens, like the library after its split) |
| --- | --- | --- |
| 32K context, `read_file` 400 lines / 20K chars | 0/2 | 0/3 |
| **64K context, `read_file` 2,000 lines / 90K chars** | **3/3** | **3/3** |

Raw output: `runs/20260914-152732/`, `runs/20260914-153113-ctx64k-read2000/`. With the default limits, every run read
page 1, got "(524 lines total; continue with start_line=401)", and answered without continuing; the 20K-character cap
also cut the middle of the page. With the larger limits Qwen read the whole file in one call and picked the newer
entry by itself (25–65 s per question). Shrinking the file alone did not help, because the relevant entry was still
past the first page.

Phase 1 implications:

- **Size `read_file` to the context window.** At 64K, return whole files up to a token budget (~20K tokens) instead
  of 400-line pages; Qwen does not reliably follow "continue with start_line" hints. Keep search for anything larger,
  and have library project configs say "search every mention; newest dated entry wins".
- **Sensitive writes need a guard, not just instructions.** Both models broke the "no sensitive details" rule. Writes to
  sensitive categories (health, finance, relationships, identity) should require approval by policy, with the diff
  shown on the phone.
- Surgical edits and dated notes are already reliable; long one-line paragraphs remain fragile for gpt-oss.

## Memory finding (blocks always-on inference)

Every llama-server process commits roughly the model's size in system memory even when the weights live in VRAM
(12.5–14.7 GB private bytes; unchanged with `--load-mode none` or `dio`, so it is not mmap). This looks like WDDM
charging GPU allocations against commit. With ~22 GB already committed at idle (WSL/Docker/apps) and an
auto-managed page file of only ~3 GB, total commit reached 36.7/37.8 GB and Claude Code's low-memory watchdog killed
the bake-off twice. Physical RAM was not exhausted. Fix candidate: a larger fixed page file on the C: SSD
(admin + reboot, user decision).
