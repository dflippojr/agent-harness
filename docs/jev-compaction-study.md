# fast-jev-compaction study: score-and-prune tool history as a selector for #155

Status: research for issue #172, written 2026-10-01. Local replay only. No transcript left the machine, no TypeSafe
credential or hosted call was used, and no dependency or compaction behavior was changed. The recommendation is the
owner's input to #155; it is limited to "borrow the idea" or "ignore" (decision 4).

## Recommendation

**Borrow the idea, not the tool, and only as a later opt-in selector behind #155's receipts.** A relevance selector
must never touch results the harness already protects: errors stay verbatim. In this replay the Jev-style selector
removed an `ok=false` result, which is an automatic no-go (decision 4) for using it as-is. The corpus is tiny and
biased (see Limitations), so this is a direction, not a measured win.

## Upstream method and provenance

- Repository: <https://github.com/tamaratran/fast-jev-compaction> (MIT). Latest commit read via the GitHub API on
  2026-10-01: `e3f262a7f4d42bd8dd32ced30d26176f7cb545b0` (2026-09-17). A Claude Code plugin/function hook (npm) by
  Tamara Tran, using TypeSafe's hosted Jev System One model. It scores every old tool call and result in parallel;
  surviving messages stay verbatim; it falls back to the host's built-in summary if Jev fails or cannot reduce
  enough. (Source: an AlphaSignal write-up; the repository page itself returned 404 to the fetch tool, so repo
  internals were not read directly.)
- Documented protocol (LiteLLM TypeSafe guardrail docs, <https://docs.litellm.ai/docs/proxy/guardrails/typesafe>):
  scorer input is the last user message (task), the system text plus each candidate's tool call and result truncated
  to 4000 chars (state), and a binary "is this exchange still needed" question. The output is a probability; results
  below `relevance_threshold` (default 0.2) are replaced with a removal notice while the tool-call row stays.
  Exchanges under 200 chars are not evaluated. System messages, the last user message and the latest
  assistant/tool exchange are never evaluated. All-or-nothing per exchange.
- Access status: the TypeSafe waitlist is gone and Jev is open (owner update 2026-09-28). Terms, retention and egress
  findings belong to #160 and #171 (`docs/jev-ultrafast-study.md`) and are not repeated here. Hosted scoring would
  need synthetic data and a separate review under #160.

## Method

- **Data:** the live events table (`harness.sqlite3`), opened read-only; 58 sessions. Tool turns are rebuilt from
  `assistant`, `tool_call`, `tool_result` events, not from `sessions.context` (already compacted). Event output is
  capped at about 20,000 chars; truncated results in the replayed sessions: 0.
- **Definitions:** error = `tool_result.ok == false`. Repeated failed call = an identical tool name and args that
  failed and appear again. Re-read = identical args to a read-type tool (`read_file`, `read_service_config`,
  `list_files`, `session_read`, `memory_read`) seen twice. Sort key = last event timestamp.
- **Corpus result:** the owner rule (20 most recent sessions with an error, a repeated failed call *and* a re-read)
  matches **0 sessions**, and I did not relax it silently. As a clearly labelled exploratory fallback I replayed the
  5 multi-turn sessions meeting *at least one* criterion (S1-S5, opaque IDs sha256[:8]: 88fbb514, ec41d6a8,
  6f39b5fd, f9752220, ac49b51e; one more matching session had a single call and was skipped). This is far from
  the intended corpus.
- **Policies, applied at every turn to the reconstructed messages:** (a) real `compaction.elide()` with defaults,
  applied unconditionally (production only runs it over a budget, so savings are an upper bound); (b) real
  `compaction.mask_used_results()` with the #155 default 2000-char minimum; (c) Jev-style selector: the protocol
  above reimplemented with local Qwen3.6-35B-A3B-UD-Q4_K_XL on 127.0.0.1:8090 (llama.cpp b10950 per the supervisor
  script), temperature 0, no thinking, 1 token, P(YES)/(P(YES)+P(NO)) from top-10 logprobs, threshold 0.2, state
  truncated to 4000 chars, replacement `[Tool result removed: judged no longer relevant]`. Task text = last user
  message. The prompt wording is mine (the upstream prompt is not public), so it is an assumption.
- **Metrics (#159 style, definitions still open there):** chars saved (sum over turns of context chars; tokens ~
  chars/4); "needed again" = a modified item whose identical call (name + args hash) recurs within 10 turns after the
  item's turn and after the prune; **ambiguous** = same tool and same first string argument (e.g. path) but
  different other args; error safety = any `ok=false` result removed or shortened; cache reuse = estimated
  analytically as the share of the previous turn's context chars that form an unchanged prefix of the new turn's
  context. It is not a measured provider cache hit.
- Reproduce: `python docs/jev-compaction-study/replay.py` from the repo root with `HARNESS_DB` set to the daemon's `harness.sqlite3` (opened read-only; needs a local Qwen on
  :8090; `--no-qwen` skips the selector). Raw aggregate output: `docs/jev-compaction-study/results.txt`. The script
  prints only aggregates, and this doc contains no transcript excerpts.

## Results (5 sessions, 574 Qwen scoring calls)

| Policy | Context chars saved | Items modified | Needed again | Ambiguous | Error results modified | Prefix reuse (est.) |
|---|---|---|---|---|---|---|
| `elide()` | 64.6% | 13 | 0 | 0 | 1 (truncated, not removed) | 87.1% |
| #155 mask (receipt) | 70.6% | 9 | 1 | 0 | 0 | 89.7% |
| Jev-style (Qwen) | 78.5% | 30 | 3 | 0 | 1 (removed) | 84.3% |

Cost/latency (reported, no go/no-go threshold): 574 scoring calls, mean 0.46 s, max 17.5 s, total 265 s for about
2.4 M replayed chars of history, 0 request failures after the first retry. The max is consistent with a cold or
restarted server: the supervisor log shows the Qwen server exited and was restarted by its supervisor (not by me)
during my session, and an earlier run of the script died with a connection reset. Each turn re-scores every old
exchange, so cost grows roughly with turns x exchanges; a hosted model would trade this for network egress.

## Reading the results

- The selector saves the most but modifies about 3x more items than #155 masking. Recurrence is 3 of 30 vs 1 of 9
  (10% vs 11%), so no clear difference at this sample size.
- The one removed error is the safety finding: relevance scoring has no built-in "errors stay" rule, and #155's
  rule 2 (never mask errors) would have to be applied around any selector. Operational test for "cause removed": the
  `ok=false` result and the `tool_call` that produced it must both survive verbatim. Calls survive under all three
  policies, so only the result matters.
- Prefix reuse is lower for the selector because it rewrites old, middle messages as scores change turn to turn,
  whereas receipts are deterministic and replace an item once. This is an analytic estimate; #159 has not recorded
  real per-turn cache counts, and hosted provider caches may behave differently.

## Limitations

- Sample: 5 sessions, none meeting the strict rule; small, selected for errors, and says nothing about typical
  sessions.
- "Needed again" is a proxy, not causal: the recorded transcripts were produced under the old `elide()` (or none), so
  recurrence reflects that policy's behavior, not what an agent would do after a different prune. A recurrence that
  follows an `elide()` "re-run the tool" marker is not separated out; 7 compaction events exist in the whole table.
- Local Qwen with my own prompt is not Jev System One; scores could differ materially, and llama.cpp output can vary
  across builds (b10950 recorded). Elide counts truncation as a "modified" item.

## Implications for #155 and #156

- #155: keep receipts as the deterministic default. If a selector is added later, it should only choose *which*
  successful, large, old results get a receipt (never errors) instead of replacing them with removal notices, so the
  full content stays recoverable via `read_artifact`. Fail open when the scorer is unavailable.
- #156: a scratchpad could hold what the selector would otherwise have to guess is still needed; not studied here.
- #159: replace the analytic cache figure with real per-turn cache counts once #159 records them.
