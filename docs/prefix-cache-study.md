# Prompt-prefix stability and llama-server cache reuse (#774)

Run 2026-10-30 07:15 to 08:41 UTC on the RTX 4070 Ti Super 16 GB / 32 GB RAM tower, on Ubuntu, under the owner's GPU
hold for #772-#774 (one GPU job at a time). Production Qwen (`agent-harness-main-llama.service`, port 8090) stayed
parked: the pause flag was still there and no production container was running at each check, and the unit and its
drop-ins were not touched. Every measurement used a throwaway container on `127.0.0.1:8082`, stopped at the end. All
sessions are synthetic canary tasks; no owner session or transcript was read. Raw output is in the gitignored
`runs/ah774/`.

The host was not quiet: other agent sessions ran tests (load average about 11). Prefill rates ranged from 486 to
779 tok/s across sessions with prefill to measure.

## Answer

- **Reuse is near 100% until something rewrites the history.** Across 7 sessions at the production 64K window, reuse
  ranged from 38% to 96% (51% over all requests, pulled down by one long session). Without a break, a request reuses
  the whole previous prompt and reply minus 1 token. The chat template re-renders the stored assistant turn, including
  its reasoning, exactly as generated.
- **Observation masking is the breaker that matters.** It caused 35 of the 36 breaks at 64K, re-reading about 260,000
  tokens beyond the new messages (about 350 s against 725 s of total prefill). The worst session broke the prefix on
  nearly every turn: 38% reuse, 638 s of prefill against 171 s of decode. Follow-up: #855.
- **The cost comes from how this server rolls back.** Qwen3.6's recurrent layers can't be cut back to the first
  differing token. The server restores a context checkpoint, and it only makes those near the end of a prompt. A
  masked result sits behind those, so 93% of the re-read tokens (about 243,000) matched the previous prompt and could
  not be reused. Follow-up: #856 (checkpoint spacing).
- **The suspected system-prompt breakers don't fire mid-round.** The system prompt and tool schemas were byte-identical
  on every request of all 14 sessions, there is no date or time in them, and the `{base_branch}` and repo-map rewrites
  only run before the first request or at a round reset (from the code; no measured session has a repository).
- **Plan placement matters (#641).** With `planning.reinject` on, a turn's median prefill went from 0.18 s to 1.33 s.
  The flag is off, so nothing needs fixing unless it is turned on. The comment is on #641.

## Reuse per session

`cache_tokens / prompt_tokens` for each request, in order. The persisted `turn_metrics` rows (`--keep-metrics`) match
the captured counts exactly: 189 turn rows over the 14 sessions. Summary requests are captured but are not turns.
A session's first request reuses about 2,000 tokens or none, depending on whether a checkpoint from an earlier session
survives in the shared system prompt and tool schemas.

**Production window, 65,536 tokens:**

| Session | Passed | Requests | Prompt tokens | Cached | Reuse | Prefill s | Decode s | Per-request reuse, % |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `log_correlation` | yes | 10 | 54,942 | 44,300 | 81% | 19.0 | 24.4 | 0 99 53 96 99 85 31 99 100 100 |
| `sqlite_report` | no | 5 | 19,345 | 17,562 | 91% | 3.9 | 20.9 | 78 81 96 95 96 |
| `merge_conflict` | yes | 9 | 31,668 | 30,476 | 96% | 4.5 | 21.0 | 79 97 96 98 96 99 97 99 98 |
| `cli_json_output` | yes | 19 | 115,753 | 102,744 | 89% | 20.2 | 62.4 | 77 98 76 99 99 100 100 100 100 86 100 96 100 90 24 96 100 99 78 |
| `duration_parser` | yes | 11 | 66,435 | 42,799 | 64% | 32.2 | 74.4 | 82 98 86 99 99 100 80 26 86 22 25 |
| `multi_bug_inventory` | yes | 7 | 30,456 | 26,711 | 88% | 7.1 | 32.5 | 98 98 78 49 87 99 98 |
| `log_correlation`, second run (debug log on) | yes | 44 | 754,158 | 282,850 | 38% | 638.0 | 171.2 | 95 99 53 98 99 85 14 36 100 97 99 100 50 15 15 14 14 14 13 13 13 12 12 12 11 11 11 11 10 10 10 14 70 65 64 62 61 60 59 58 10 14 100 99 |
| all | | | 1,072,757 | 547,442 | 51% | 724.9 | 406.9 | |

The same task can differ a lot between runs: `log_correlation` took 10 requests once and 44 the next time. In the
second run the model re-ran a `grep` whose output was 16.5K characters, about 7,600 tokens, again and again. Each result
was masked a turn later.

**Small window, to force compaction.** No session reached `elide_at` (36K tokens) on its own in the first set of runs,
so these two ran with `context_tokens: 12288` in a config copy (the server stayed at 65,536). The second
`log_correlation` run above did get there at the real window, with one elide. A 12K window compacts far more often than
production does, so read this table for the kinds of break, not for how often they happen.

| Session | Passed | Requests | Prompt tokens | Cached | Reuse | Prefill s | Decode s | Per-request reuse, % |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `log_correlation` | yes | 16 | 81,519 | 39,304 | 48% | 57.4 | 60.6 | 79 81 81 59 38 99 100 0 23 42 0 22 66 0 25 61 |
| `multi_bug_inventory` | yes | 6 | 23,096 | 17,714 | 77% | 8.3 | 19.8 | 81 98 72 81 42 95 |

**`planning.reinject` on, 64K window.** The plan only appears after the model calls `update_state`, which among these
tasks only `cli_json_output` did. The other two rows are in effect more baseline samples. `cli_json_output`'s third run
stalled (see "What this did not settle"); its 13 requests are counted here but not in the plan comparison.

| Session | Passed | Requests | Prompt tokens | Cached | Reuse | Prefill s | Decode s | Per-request reuse, % |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `cli_json_output`, run 1 | yes | 16 | 92,058 | 74,318 | 81% | 28.1 | 62.5 | 76 98 76 99 97 90 88 86 86 77 76 79 25 87 91 92 |
| `cli_json_output`, run 2 | yes | 19 | 108,156 | 84,642 | 78% | 36.1 | 48.3 | 76 98 76 99 98 94 90 88 86 86 77 82 82 28 85 26 90 90 79 |
| `cli_json_output`, run 3 (debug log on, stalled) | no | 13 | 56,597 | 48,047 | 85% | 18.2 | 27.4 | 0 98 76 99 98 94 90 88 86 86 84 87 88 |
| `log_correlation` (no plan saved) | yes | 11 | 91,612 | 30,054 | 33% | 80.0 | 29.2 | 78 81 97 99 79 17 21 19 18 25 17 |
| `multi_bug_inventory` (no plan saved) | yes | 6 | 25,467 | 19,345 | 76% | 9.4 | 37.3 | 98 98 72 81 34 96 |

## What breaks the prefix

"Beyond new" is the tokens the server read minus the messages the request added, which any server would have to read.
Each is priced at that session's own prefill rate (`prompt_ms` per token over requests that read at least 256 tokens,
about 1.3 ms). "Matched, not reused" is the part that was the same in the previous prompt but sat after the last usable
checkpoint.

| Sessions | Class | Breaks | Beyond new, tokens | Largest | Estimated s | Matched, not reused |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 64K | masking: observation receipt | 35 | 259,595 | 14,592 | 349.5 | 242,575 |
| 64K | compaction: elide dropped reasoning | 1 | 13,877 | 13,877 | 18.7 | 118 |
| 12K | compaction: summary request | 3 | 6,350 | 3,102 | 8.5 | 9 |
| 12K | compaction: the request after a summary (estimated) | 3 | about 3,700 | about 1,300 | about 5 | not measured |
| 12K | masking: observation receipt | 4 | 6,368 | 2,687 | 8.6 | 4,351 |
| 12K | compaction: elide dropped reasoning | 1 | 2,701 | 2,701 | 3.6 | 515 |
| Flag on | masking: observation receipt | 10 | 37,772 | 5,904 | 51.8 | 32,059 |
| Flag on | injected state: plan re-injection | 30 | 17,369 | 1,635 | 27.7 | 10,876 |

The three requests right after a summary read 6,598, 6,949 and 7,607 tokens, mostly a new result of 12K to 14K
characters. These captures can't be re-rendered without a server, so the new result is estimated: the next request
masks it, so the prompt's drop, plus that turn's reply and new messages and the receipt, gives its tokens (about 5,300,
5,650 and 6,500, or 2.2 characters a token). The rest, about 1,300, 1,300 and 1,100 tokens, is priced at that session's
1.34 ms per token. The receipt is taken as 170 tokens, so read these as good to a few hundred tokens each.

The masking estimate is
an upper bound on what a fix could save. Without masking, those results stay in the prompt and
the context fills sooner.

Each class from the issue:

| Class | Found | Can it move to the end, or out, without changing behaviour? |
| --- | --- | --- |
| `{base_branch}` fill-in | Not a break. `_first_prepare` fills it before the first model call, once (from the code). | Nothing to move. |
| Repo-map and workspace-guidance rewrite | Not a break mid-round. Applied before the first call and again at a round reset, which rewrites the context anyway (from the code; the repo map is off by default). | Nothing to move. |
| Dates or times | None in the system prompt or tool schemas. | Nothing to move. |
| Tool-schema order | Stable: one variant per session. | Nothing to move. |
| Injected state (`update_state`) | Not a break with the flag off: state is an appended tool call and result. | Nothing to move. |
| Plan re-injection (flag on) | A break on every turn after the first `update_state`. | Not as designed: the block sits before the reply that the next request re-reads. See below. |
| Masking | The main cost. | Not to the end, since it rewrites a message in place. It can be batched: #855. |
| Elide | One break, early in the history (it strips reasoning from older turns): 21,444 tokens read at the real window, about 19 s beyond the new messages. | Not worth a change alone. It is rare, happens only under pressure, and heads off a summary request. |
| Summary and round reset | Breaks by design (`docs/compaction.md`). | No. |
| Assistant turn re-rendered | Not a break: every unbroken pair reuses the whole reply minus 1 token. | Nothing to move. |

## Why a break costs this much

From the server's debug log (`-lv 4`):

```
load_model: context checkpoints enabled, max = 32, min spacing = 8192
created context checkpoint 4 of 32 (pos_min = 30556, pos_max = 30556, n_tokens = 30557, size = 62.813 MiB)
erasing context checkpoint too close to an earlier one (pos_min = 31068, pos_max = 31068, n_tokens = 31069, ...)
restored context checkpoint (pos_min = 11492, pos_max = 11492, n_tokens = 11493, n_past = 11493, size = 62.813 MiB)
```

- Each prompt gets checkpoints only near its end: n-516 and n-4. The next prompt erases those closer than 8,192 tokens
  to an earlier one.
- A request whose prompt extends the cached one reuses all of it.
- A request that differs earlier restores the last checkpoint at or before the difference, and re-reads the rest.

So a mask, one turn behind the newest result, lands behind both end checkpoints and falls back a long way: to token
1,969 or 2,366 in the shorter sessions, whatever the depth of the break. A rewrite that lands in the last 516 tokens
falls back only to n-516, which is what the plan block does.

Prompts built up turn by turn keep checkpoints roughly 8,192 tokens or more apart, while a prompt read in one go has
none in the middle. Measured with `break_at_scale.py`: a 40,703-token conversation of tool results, one result replaced
by a receipt. The re-read requests ran at about 800 tok/s.

| Rewrite at | Built up one result at a time: cached, read, s | Read in one go: cached, read, s |
| --- | --- | --- |
| 90% | 30,557, 7,035, 9.1 | 1,969, 35,623, 44.3 |
| 50% | 11,493, 26,080, 33.4 | 2,366, 35,207, 43.4 |
| 10% | 2,366, 35,224, 43.7 | 1,969, 35,621, 43.8 |

Sending the unchanged prompt again read 4 tokens in about 65 ms. A checkpoint at n-4 from the earlier read can also
restore a whole prompt: four of the six refills that followed a masked request did this, and the other two re-read
10,146 and 38,337 tokens. The debug log also shows the host-RAM prompt cache (`--cache-ram`) holding
one or two prompts during these runs. None of the measured restores needed it to explain them; its effect was not
isolated.

## Plan re-injection (#641)

`docs/compaction.md` said each call re-reads the plan plus the new turn. Measured, it re-reads more. The server holds
prompt N, plan included, and reply N. Request N+1 has the same history without that plan, so it departs at the plan's
first token, and everything after that goes too: reply N is read again, not reused, and the fallback is to n-516. The
fallback landed 425 to 432 tokens before the plan's start in 17 of the 22 turns.

| `cli_json_output` | Turns measured | Median beyond new, tokens | Median prefill, s |
| --- | ---: | ---: | ---: |
| Flag off, no break | 17 | 8 | 0.18 |
| Flag on, turns that carry the plan, run 1 | 10 | 550 | 1.33 |
| Flag on, turns that carry the plan, run 2 | 12 | 517 | 1.33 |

That is about 1.1 s more on each turn. `cli_json_output`'s session prefill rose from 20.2 s to 28.1 s and 36.1 s, part
of which is masking breaks (one, one and two of them). Moving the block further up would cost much more, as the masking numbers show. Sending it only when the plan
changes, or storing it as a real message, would avoid the per-turn break, but both change what the experiment tests.

## What the live daemon's counter says

`harness_prompt_cache_tokens_total` on the live daemon read 159,848 cached and 253,033 recomputed at 08:42 UTC (39%),
all sessions it has recorded. That is well below most of the sessions here. It happens to match the 38% of the
long `log_correlation` run, but this study does not show why: real sessions also wait on approvals and on the model's
idle sleep (`--sleep-idle-seconds 600`, left out of the throwaway server), and per-session numbers were out of scope.

## What this did not settle

- **Real sessions:** none were read. Repository projects, follow-up messages, long waits and the idle sleep were not
  exercised, and neither was the `{base_branch}` path at run time.
- **The stall:** `cli_json_output`'s third run stopped after turn 13 and sat until the runner's cap, then 45 minutes
  (`still waiting for the GPU at the run's cap`, recorded as suspended). Its last call was a harmless `run_shell`. The
  data dir is deleted at the end of a run, so the cause was not found. It happened once, with debug logging on and other
  work on the host.
- **Debug logging:** the second `log_correlation` run, the third `cli_json_output` run and the built-up `break_at_scale`
  table ran with `-lv 4`. Rates were similar on the at-scale re-reads (785 tok/s with it, 811 without), but these are
  not strictly the same server configuration as the rest.
- **The daemon restart:** the production daemon restarted at 07:39:43 UTC, following a merge to `main`. One measurement
  spanned it, the built-up `break_at_scale` run (07:37:56 to 07:40:36), during its 50% rewrite. That request read at
  1.28 ms per token against 1.30 and 1.31 for the two before it. The run used its own server, data dir and checkout, and
  the pause flag was not touched, so it was not rerun. All other sessions finished by 07:32:45 or started after 07:41.
- **The `grep` loop's cause:** whether re-running that command is ordinary for this model was not studied. It is one
  session, and it weighs heavily in the 64K totals. Without it, the other six sessions reuse 83% and masking costs
  far less, about 38 s across 6 breaks.
- **Summary prompts' exact rendering:** these captures predate recording template options, so the summary calls were
  rendered without `enable_thinking: false`. Nothing here depends on it: their token counts are the server's own, and a
  summary call differs from its neighbours at the first message whatever the option does. No capture has a failed call.
- **A flag change:** nothing here tried other llama-server flags (out of scope). #856 does.

## Commands

From the repository root, with the project's Python dependencies and Docker. `--keep-metrics` and `--capture-prompts`
are off by default and change nothing else. `run_standalone` builds its own config with the guard off, so it never
touches the pause flag.

The issue asks for the daemon to be stopped. It stayed up here, because the runner uses its own data dir and this
server, and the daemon's model stayed parked.

```bash
mkdir -p .tmp/ah774

# A config copy aimed at the throwaway server. harness.yaml is a symlink: replace it, never edit through it.
cp -r ~/.local/share/agent-harness/config .tmp/config
rm .tmp/config/harness.yaml && cp -L ~/.local/share/agent-harness/config/harness.yaml .tmp/config/harness.yaml
sed -i 's#127.0.0.1:8090#127.0.0.1:8082#g' .tmp/config/*.yaml
# The 12K window: the same copy, with context_tokens: 12288 in place of 65536.

# The server: the production unit's image, model and flags, without --sleep-idle-seconds. The health check probes the
# same port the server listens on, not the image's :8080 (#858).
port=8082
docker run --rm --name ah774-llama --gpus all --network host \
  --health-cmd "curl -f http://127.0.0.1:$port/health" --health-start-period 300s \
  -v "$HOME/.local/share/agent-harness/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf:/models/model.gguf:ro" \
  ghcr.io/ggml-org/llama.cpp:server-cuda-b10830 \
  -m /models/model.gguf --alias qwen3.6-35b-a3b --host 127.0.0.1 --port "$port" \
  --ctx-size 65536 --flash-attn on --parallel 1 --jinja --metrics \
  --fit on --cache-type-k q8_0 --cache-type-v q8_0
# For the checkpoint log add -lv 4, with --log-driver none on docker run, and filter the output with grep.

# One session per invocation, so each capture file holds one session. Add --plan-reinject for the flag rows.
for t in log_correlation sqlite_report merge_conflict cli_json_output duration_parser multi_bug_inventory; do
  HARNESS_CONFIG_DIR=$PWD/.tmp/config python -m bakeoff.canary --tasks $t --repeats 1 \
    --keep-metrics .tmp/ah774/$t.metrics.json --capture-prompts .tmp/ah774/$t.prompts.jsonl
done

# First differing message and token. render needs the server; report is offline.
python -m bakeoff.prefix_diff render .tmp/ah774/$t.prompts.jsonl .tmp/ah774/$t.rendered.jsonl --server http://127.0.0.1:8082
python -m bakeoff.prefix_diff report .tmp/ah774/$t.rendered.jsonl --json .tmp/ah774/$t.report.json

# One break on a long prompt.
python docs/prefix-cache-study/break_at_scale.py .tmp/ah774/log_correlation.prompts.jsonl .tmp/ah774/scale.json [--grow]

docker stop ah774-llama
```

The harness side also runs without a GPU: `tests/test_prefix_cache_study.py` drives the real runner with the scripted
model and checks that tool schemas stay the same, and that the only breaks are masking, round resets and, with the
flag on, the plan.

## Follow-ups

- #855: batch observation masking so it stops breaking the prefix on every turn. Measured on the canary in #898
  (below).
- #856: measure llama-server checkpoint spacing for these breaks. Measured below.
- A comment on #641 with the plan numbers. The flag stays off, so no fix is filed.

## Batched masking on the canary (#898)

Run 2026-11-19 16:49 to 18:33 UTC on the same tower, under the owner's GPU park for #898 and #856. Production Qwen
stayed parked (the orchestrator's manual hold and `llama-server.paused`, neither touched), and every session used a
throwaway container on `127.0.0.1:8082`, stopped at the end. All sessions are synthetic canary tasks. Raw output is in
the gitignored `runs/ah898/`.

**Setup.** The production unit's image, model and flags as they are today, which add `--fit-target 2048` to the #774
command, plus an explicit `--checkpoint-min-step 8192` (the default). Harness at this branch's base, `34c3a081`. The six
hard tasks, two attempts each per arm (`canary.yaml`'s `repeats: 2`), one session per invocation with `--keep-metrics`
and `--capture-prompts`. The arms ran interleaved: for each repeat and each task, one session per arm, in a rotating
order, so host load fell on all three alike. Load average was 1.4 to 6.2.

| Arm | What | Config |
| --- | --- | --- |
| off | today's default: each result masked the turn after it is used | `--mask-batch-at` not given |
| 0.45 | batched masking at 45% of the window | `--mask-batch-at 0.45` |
| 32K + 0.45 | the more aggressive arm from the 2026-11-18 comment (arXiv 2611.05306): compact sooner, keep less | `context_tokens: 32768` in the config copy (the server stays at 65,536), and `--mask-batch-at 0.45` |

The canary runner builds its own `Config`, so `elide_at`, `summarize_at` and `keep_recent` in a config copy don't reach
it. The model's `context_tokens` does, and every compaction threshold is a share of it, so halving it moves elide,
summary, `keep_recent` and the batch threshold to half the tokens together.

**Totals**, over the attempts graded in each arm:

| Arm | Passed | Requests | Prompt tokens | Reuse | Prefill s | Decode s | Wall s | Breaks | Prefill s per session |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| off | 9/11 | 112 | 675,612 | 74% | 273 | 453 | 742 | 18 | 24.8 |
| 0.45 | 10/12 | 137 | 1,041,941 | 92% | 140 | 556 | 718 | 1 | 11.7 |
| 32K + 0.45 | 9/11 | 101 | 640,939 | 76% | 223 | 386 | 623 | 9 | 20.3 |

Breaks here are agent requests that reused less than 90% of the previous agent prompt (`arm_report.py`). No arm made a
summary call. Wall time is the canary's GPU time, which with nothing else queued is the attempt's wall time.

**Per task**: passes, prefill seconds of each attempt, and reuse over both attempts.

| Task | off | 0.45 | 32K + 0.45 |
| --- | --- | --- | --- |
| `log_correlation` | 2/2, 73 + 45, 47% | 2/2, 15 + 48, 73% | 2/2, 81 + 73, 37% |
| `duration_parser` | 2/2, 57 + 56, 73% | 2/2, 8 + 23, 97% | 2/2, 24 + 11, 91% |
| `multi_bug_inventory` | 2/2, 9 + 9, 76% | 2/2, 5 + 6, 89% | 2/2, 4 + 5, 89% |
| `cli_json_output` | 1/1, 7, 96% | 2/2, 8 + 9, 97% | 1/1, 6, 95% |
| `merge_conflict` | 2/2, 6 + 5, 97% | 2/2, 5 + 5, 96% | 2/2, 4 + 5, 96% |
| `sqlite_report` | 0/2, 4 + 3, 91% | 0/2, 3 + 5, 94% | 0/2, 4 + 4, 93% |

**Prefill and decode against context length**, all requests of an arm by their prompt size. Prefill rate is the tokens
the server read over `prompt_ms`; seconds per request is prefill plus decode.

| Prompt tokens | off: prefill tok/s, decode tok/s, s/request (n) | 0.45 | 32K + 0.45 |
| --- | --- | --- | --- |
| 0-8K | 534, 74.2, 5.0 (86) | 407, 73.2, 4.7 (91) | 451, 73.4, 4.8 (73) |
| 8-16K | 725, 69.4, 11.4 (26) | 663, 71.7, 5.7 (32) | 754, 71.6, 8.4 (26) |
| 16-24K | | 660, 69.7, 5.0 (12) | 763, 69.1, 19.1 (2) |
| 24-32K | | 745, 68.3, 12.7 (2) | |

What it says:

- **Batching at 0.45 halves prefill with no drop in pass rate.** Prefill fell from 273 s to 140 s (131 s over the 11
  attempts that have a pair in the off arm, -52%), reuse rose from 74% to 92%, and prefix breaks from 18 to 1. Every
  task passed or failed the same way in both arms: `sqlite_report` failed every attempt in every arm, as it has in every
  recorded run (see `docs/canary-evals.md`), and every other graded attempt passed.
- **The gain is where masking used to break the prefix.** `duration_parser` went from 113 s of prefill over two
  attempts to 31 s, `log_correlation` from 118 s to 63 s and `multi_bug_inventory` from 18 s to 11 s. Tasks that never
  masked much (`merge_conflict`, `cli_json_output`, `sqlite_report`) are the same within noise.
- **Wall time barely moves, because decode dominates at these sizes.** Decode was 453 s against 273 s of prefill with
  masking off. With batching, sessions kept more of each result and wrote more (40,335 completion tokens against
  33,047; the second `duration_parser` attempt took 27 requests), so the arm's wall time fell only from 742 s to 718 s
  (654 s over the paired 11). The prefill saving per request at 8-16K tokens, 11.4 to 5.7 s, is what the owner would
  notice on long sessions.
- **The cost is window space.** Batched prompts are larger (1.04 M prompt tokens against 0.68 M, and a 26K-token peak
  against 16K), almost all of it reused. That is the expected trade from the trim bench (`docs/compaction.md`).
- **The aggressive arm is not better.** At a 32K window and 0.45, `duration_parser` and `multi_bug_inventory` gained
  about as much as at 64K, but `log_correlation` lost: its results are about 7,600 tokens each, so at a 14.7K-token
  threshold every new result crosses it and masking breaks the prefix as often as with batching off (37% reuse, 154 s
  of prefill). Fewer tokens per request did not buy anything visible here: no session came near the 64K window
  (largest prompt 26K tokens), and decode slowed only from about 74 to 68 tok/s between the smallest and the largest
  prompts. The paper's cost growth with position is real, but these tasks never reach the positions where it bites.
- **Recommendation: flip the default to `compaction.mask_batch_at: 0.45`,** in a separate PR once the owner agrees. Keep
  the window at 64K; don't adopt the smaller window. The evidence is two attempts per task: enough to see a halving of
  prefill on the tasks that mask, not enough to rule out a small pass-rate effect.

**Three attempts are left out, all `cli_json_output`, and they show a canary bug.** Each stopped on a shell command
such as `cd /workspace && rm -f /tmp/test.json && python -m pytest tests/ -v`. The policy asks before any `rm` in a
command that also has a `cd`, since a relative target depends on the working directory (`harness/policy.py`,
`_delete_outside_scratch`), even though `/tmp` is scratch. A canary session has nobody to approve, so the session
waited, its sandbox was idle-stopped, and nothing happened until the run's cap. The runner then records the attempt as
`suspended` with the note "still waiting for the GPU at the run's cap" (one ran to the cap; two were interrupted once
the pattern was clear). The canary's rule leaves suspended attempts out of the pass rate, so the arms are compared over
the 11, 12 and 11 attempts that finished. One attempt with masking off and two (one a rerun) with the 32K window
stalled this way, and none with 0.45. This is very likely the cause of the stall #774 could not explain
(`cli_json_output`'s third run, same task, same note, after turn 13). It is a follow-up, not part of this change: the
nightly canary counts these attempts as neither pass nor fail, and its note blames the GPU.

## Checkpoint spacing (#856)

Run 2026-11-19 18:34 to 19:56 UTC, right after #898, under the same park and on the same tower. Raw output is in the
gitignored `runs/ah856/`.

**Method: the #898 prompts replayed at each spacing.** Canary tasks vary too much between attempts (`duration_parser` took 11 requests,
the next 27) to compare three settings, so every spacing read the same prompts:
`python -m bakeoff.checkpoint_replay` sends each captured request of the 35 #898 sessions again, in order, with the same
messages, tools and template options, generating one token. The sessions are the 34 graded ones and one that was
interrupted at an approval (its 14 requests are real prompts too). Each pass got a fresh server, with the #898 flags and
`--checkpoint-min-step` 8192 (the default), 2048 or 512. The passes ran in the order 8192, 2048, 512, 512, 2048, 8192,
so host load drift fell on every setting alike. The server's own `timings` give the tokens read, the tokens reused and
`prompt_ms`.

One deviation from the live run: there the server generated each reply, here it generates one token, so the next
prompt reads the reply as prompt tokens. That adds the same tokens at every spacing, and it is why a replay's prefill
is higher than the live run's.

**Totals**, 364 requests and 2,425,805 prompt tokens per pass:

| `--checkpoint-min-step` | Tokens read | Reuse | Prefill s, pass 1 and 2 | Peak anonymous memory, MiB | Peak swap, MiB |
| --- | ---: | ---: | --- | --- | --- |
| 8192 (default) | 491,663 | 79.7% | 766, 748 | 8,530, 8,440 | 1,717, 498 |
| 2048 | 413,513 | 83.0% | 681, 657 | 8,918, 8,856 | 167, 281 |
| 512 | 391,587 | 83.9% | 619, 622 | 9,044, 9,163 | 626, 161 |

Tokens read were identical in both passes of a spacing, to the token, so the replay is deterministic. Only the seconds
vary with host load.

**By #898 arm**, the mean of both passes:

| #898 arm | 8192: read, prefill s | 2048 | 512 |
| --- | --- | --- | --- |
| off (today's masking) | 194,748, 288 | 139,011, 214 | 126,873, 197 |
| 0.45 (batched) | 118,298, 201 | 115,851, 214 | 115,335, 196 |
| 32K + 0.45 | 178,617, 268 | 158,651, 241 | 149,379, 228 |

**A built-up 40K-token prompt** (`break_at_scale.py --grow`, one result rewritten at 90%, 50% and 10% of its length;
tokens the server reused, then seconds to re-read):

| Rewrite at | 8192 | 2048 | 512 |
| --- | --- | --- | --- |
| 90% | 30,557, 11.1 s | 30,557, 9.6 s | 30,557, 9.6 s |
| 50% | 11,493, 43.6 s | 17,848, 26.2 s | 17,848, 26.3 s |
| 10% | 2,031, 109.8 s | 5,146, 42.5 s | 5,146, 42.6 s |

The 8192 row's 10% re-read ran at about 320 tok/s against about 760 at the other two; host swap peaked during that
pass, so read its seconds as noisy and the reused tokens as the result. Each grown step adds about 1,400 tokens and the
server leaves checkpoints only near each prompt's end, so a spacing below that keeps nothing more: 512 equals 2048.

What it says:

- **With today's per-turn masking, a smaller spacing helps a lot.** On the masking-off sessions, 2048 read 29% fewer
  tokens and took 26% less prefill; 512 read 35% fewer and took 32% less. The gain is where #856 expected it: sessions
  with small results between the masked ones, such as `duration_parser` (44,000 tokens read at 8192, 18,000 to 25,000
  at 512) and `log_correlation`.
- **With batched masking it adds nothing measurable.** On the 0.45 sessions, 512 saves 3,000 of 118,000 tokens read
  (2.5%), and the prefill seconds (201, 214, 196) are within the passes' noise. Batching removes the breaks that
  checkpoint spacing would soften.
- **RAM:** peak anonymous memory rose by about 0.4 GiB at 2048 and 0.6 GiB at 512, against 8.5 GiB at the default.
  Most of that base is llama-server's host prompt cache (`--cache-ram`, 8 GiB by default), which the production flags
  leave as it is. The ceiling for checkpoints is 32 per slot, about 2 GiB, at any spacing; a smaller spacing lets more
  of them survive. The mmap'd model is file cache and isn't counted. Swap peaks swung from 161 to 1,717 MiB
  between passes, and swapped-out pages don't count as resident, so these differences are approximate.
- **Go or no-go: no-go while the #898 recommendation is adopted.** With `mask_batch_at: 0.45` the change buys nothing
  for up to 0.6 GiB of RAM. If the owner keeps batched masking off, `--checkpoint-min-step 2048` is the setting to
  take: most of the gain for the smaller RAM cost. No production flag was changed.
- **Interaction with #851 (speculative decoding), not measured.** The DFlash drafter also uses checkpoints and changes
  the flags, but #851 is not adopted, and its one approved drafter run needs the owner at the GPU. So the drafter was
  not loaded. A one-token replay wouldn't show drafting's own checkpoint use anyway, since that happens during decode.
  If #851 is adopted, rerun the replay commands below with its flags before changing the spacing.

## Commands (#898, #856)

As in #774's commands above, with today's production flags. The config copies are `.tmp/config` (the copy above) and
`.tmp/config32k` (the same copy with `context_tokens: 32768`).

```bash
# Server: the #774 command plus the production unit's --fit-target 2048 and an explicit spacing.
docker run -d --rm --name ah898-llama --gpus all --network host \
  --health-cmd "curl -f http://127.0.0.1:8082/health" --health-start-period 300s \
  -v "$HOME/.local/share/agent-harness/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf:/models/model.gguf:ro" \
  ghcr.io/ggml-org/llama.cpp:server-cuda-b10830 \
  -m /models/model.gguf --alias qwen3.6-35b-a3b --host 127.0.0.1 --port 8082 \
  --ctx-size 65536 --flash-attn on --parallel 1 --jinja --metrics \
  --fit on --cache-type-k q8_0 --cache-type-v q8_0 --fit-target 2048 --checkpoint-min-step 8192

# #898: per repeat, per task, one session per arm (arms interleaved). ARM is off, b45 or w32.
#   off: HARNESS_CONFIG_DIR=$PWD/.tmp/config   (no --mask-batch-at)
#   b45: HARNESS_CONFIG_DIR=$PWD/.tmp/config   --mask-batch-at 0.45
#   w32: HARNESS_CONFIG_DIR=$PWD/.tmp/config32k --mask-batch-at 0.45
HARNESS_CONFIG_DIR=$PWD/.tmp/config python -m bakeoff.canary --tasks $t --repeats 1 [--mask-batch-at 0.45] \
  --keep-metrics runs/ah898/$ARM-$t-r$rep.metrics.json --capture-prompts runs/ah898/$ARM-$t-r$rep.prompts.jsonl
python docs/prefix-cache-study/arm_report.py runs/ah898 --json runs/ah898/report.json

# #856: a fresh server per pass, the same command with --checkpoint-min-step $step (8192, 2048, 512, then reversed).
python -m bakeoff.checkpoint_replay runs/ah856/replay-p$pass-$step.json runs/ah898/*.prompts.jsonl \
  --server http://127.0.0.1:8082 --container ah856-llama --label $step
python docs/prefix-cache-study/break_at_scale.py runs/ah898/off-log_correlation-r1.prompts.jsonl \
  runs/ah856/scale-grow-$step.json --grow
```

`tests/test_checkpoint_replay.py` covers the replay tool without a server: the requests it sends, its totals and the
memory sampler.
