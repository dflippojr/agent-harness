# Context compaction and observation masking

Local-model agent sessions keep the model's context under the window with four
steps, in order: **mask** used tool results, **trim** older tool results with
fixed rules, **elide** old tool output, then **summarize** older turns. This
page is the operator description of masking and trimming. Elide and summarize
thresholds (`elide_at`, `summarize_at`, `keep_recent`) are live admin keys; see
[`config-registry.md`](config-registry.md).

## Masking

When a successful tool result is at least `mask_min_chars` characters, the
daemon stores the **full** string in the session's `artifacts` table (keyed by
SHA-256 of that string). After the model has used that result, the next
compaction pass replaces the message in the model's context with a short
receipt. The receipt names the tool, a truncated argument preview, the
character length, the digest, and tells the model to recover the text with
`read_artifact`.

`read_artifact` reads a character range of that stored string: `start` is
inclusive, `end` is exclusive, and each call returns at most 20,000
characters. Ask for a later range if the first slice is truncated.

Identical full outputs share a digest, so one artifact row can back more than
one rewritten tool message.

## Large tool results

Masking shortens a result after the model has used it; `tool_output` bounds
keep a large result from entering the context whole in the first place.
Local-model sessions only: hosted CLI backends get their results unchanged.

| Key (`config/harness.yaml`, `tool_output:`) | Default | Applies to |
| --- | --- | --- |
| `run_shell_chars` (max `run_shell_chars_max`) | 20000 (100000) | `run_shell` |
| `verify_summary_chars` | 4000 | `verify` |
| `result_chars` (max `result_chars_max`) | 20000 (100000) | every other successful result except `read_artifact` |

A project in `config/projects.yaml` can override any of them under its own
`tool_output:` block. Over the bound, the daemon stores the whole result as an
artifact and the model gets its head and tail, then a footer naming the
artifact: `... (output truncated; N characters total; recover with
read_artifact(artifact_id=…, start=0, end=20000))`. MCP results and
`session_read` transcripts still open with their untrusted-content banner; the
artifact holds the plain text. `session_search` marks each hit from a tainted
session with that banner in the result itself, so the artifact keeps it, and
cuts each passage, answer and title (marked `… [cut]`) so ten hits fit the
default bound. A result with a tainted hit also opens with the banner, as does
a `read_artifact` range of it, so a bounded head and tail or a range that starts
past a hit's own banner stays marked (#798). Where `read_artifact` is unavailable (see
below), the result is middle-truncated with no footer. `read_artifact` itself
is never bounded or stored again.

Tool text is made valid UTF-8 where a native tool's result enters the runner,
before it is hashed, stored or shown to the model (#777): each lone UTF-16
surrogate (an MCP JSON string can carry `"\ud800"`, for example) becomes
U+FFFD, and a surrogate pair becomes its character. The digest, the artifact,
the `tool_result` event and the model's context all hold that sanitised text,
so the session database and the artifact store stay valid UTF-8.

## What the transcript and UI show

The `artifacts` table is the source of truth for the full text. Tool-result
events and the web UI show a middle-truncated view capped at 20,000
characters, so "the transcript keeps the original" is true only up to that
cap. The model context after masking holds the receipt, not the truncated
event text.

## When masking is off

No artifacts are stored and no receipts are emitted when any of these hold:

- the session kind is `chat`
- the session backend is not `local` (hosted CLI backends)
- project or app policy denies `read_artifact`

Masking and the `read_artifact` tool use the same eligibility check: a receipt
is only written when the model can actually call the recovery tool.

## `mask_min_chars`

File configuration only, in `config/harness.yaml` under `compaction:` (not a
live admin setting, not in the typed registry). Values are clamped to 1
through 10,000,000; anything outside that range falls back to 2000. Do not
treat this page as a tuning guide.

## The `mask` event

When at least one result is rewritten, the daemon emits a `compaction` event:

| Field | Meaning |
| --- | --- |
| `tier` | `"mask"` |
| `tokens_saved` | estimated tokens removed (`characters_saved / chars_per_token`) |
| `characters_saved` | characters removed from the model context |

This payload does **not** include `tokens_before`, `tokens_after`, or
`summarized_messages`. Those fields belong to the `elide` and `summary` tiers.
The web UI, CLI, and Markdown transcript render a mask event as a one-line
note using `tokens_saved` only.

## Trimming

Once the context reaches `elide_at`, and before elide and any summary request,
a free rule tier rewrites older tool results (#637). Like elide, it costs no
model call; arXiv 2609.20804 found that cheap elision ahead of the summary call
is what reduces summary calls. The rules only replace a tool result's text and
never remove a message, so every result keeps its tool call. They skip the
system prompt, the task, receipts (masking owns those), and the tail a summary
would keep verbatim: the newest turn, plus older messages while they fit in
`keep_recent` of the window. Running trim again on its own output changes
nothing.

| Rule (`compaction.trim.*`) | What it does |
| --- | --- |
| `strip_noise` | In `run_shell`, `verify` and `git_clone` output only, removes ANSI escape sequences, keeps only what a terminal would show of a line overwritten with carriage returns, and keeps the last line of a run of progress-bar lines (a percentage plus a bar of `#`, `=` or block characters). |
| `head_tail` | `run_shell`, `verify` and `git_clone` output longer than `head_lines` + `tail_lines` + 1 lines keeps its first `head_lines` (40) and last `tail_lines` (60) lines around a marker line. File reads are left alone. |
| `superseded_reads` | Drops a `read_file` result when the same file is read again later with the same arguments, or written or edited later, by a call whose result event says it succeeded (a failed reread keeps the older contents, and so do a call with no recorded outcome, as in a fork, and a reread cut to fit the turn's budget). Paths compare as the file tools read them: only the `/workspace` prefix is dropped, and a path with a `..` segment never matches (a symlink could make it name another file). The older read is dropped, not archived (gap report Section 4.3: recoverable elided content was almost never recalled); a short note names the path. |
| `dedupe` | A result of 200 or more characters that repeats an earlier one exactly becomes a one-line "same output as a later ... result below" note. The newest copy stays, so a summary (which removes the oldest messages) never keeps a note without its copy. File reads are left to `superseded_reads`, since a later pass could drop the copy a note points to. |

All rules are on by default. They are file configuration only, under
`compaction.trim` in `config/harness.yaml` (not live admin keys). A switch that
is not a YAML boolean, or a line count outside 0-10,000, keeps its default.

When trim removes anything, the daemon emits a `compaction` event before the
elide or summary event, with the same shape as the mask event:

| Field | Meaning |
| --- | --- |
| `tier` | `"trim"` |
| `tokens_saved` | estimated tokens removed (`characters_saved / chars_per_token`) |
| `characters_saved` | characters removed from the model context |

The web UI, CLI and Markdown transcript show it as a one-line note. The
trimmed context is stored with the elide or summary result of the same pass.
The following `elide` event's `tokens_before` is still the size before trim, so
`summary_averted` counts passes that trim and elide together kept under
`summarize_at`.

### Benchmark

`python -m bakeoff.trim_bench` replays three fixed synthetic transcripts
through the real compaction functions, the way `_maybe_compact` runs them for a
local agent session without saved state. The transcripts are a test-fix loop
(a failing pytest run with ANSI colour, read, edit, re-read, `git status`,
repeated), exploration (re-reading the same files and listings, mostly under
the masking threshold) and build failures (long noisy `make` logs re-run after
each fix). A summary is a fixed 2,400-character stand-in. The arms are trim off
(baseline), trim on, a config-only arm that only widens the elide/summarize
gap to 25 points (0.55/0.80, as in 2609.20804; `reset_at` 0.60 stays between
them), and both. "Chars sent" adds up every agent prompt (context plus tool
schemas) and every summary request. "Elide-only" is the share of compaction
passes that ended under `summarize_at` without a summary call. "Needed again"
counts head-and-tail trimmed outputs whose identical call recurs within 10
turns (the proxy from [`jev-compaction-study.md`](jev-compaction-study.md)).

The production window (65,536 tokens) with masking on:

| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |
|---|---|---:|---:|---:|---:|---:|---:|
| test-fix loop (202 turns) | baseline | 8,390,926 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 8,390,926 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 8,390,926 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 8,390,926 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| exploration (170 turns) | baseline | 9,699,140 | 0 | 2 | 2/2 (100%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 9,596,093 | 0 | 1 | 1/1 (100%) | 34,349 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 9,699,140 | 0 | 2 | 2/2 (100%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 9,596,093 | 0 | 1 | 1/1 (100%) | 34,349 | 0 / 0 |
| build failures (150 turns) | baseline | 5,028,257 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| build failures (150 turns) | trim | 5,028,257 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| build failures (150 turns) | config-only (0.55/0.80) | 5,028,257 | 0 | 0 | 0/0 | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 5,028,257 | 0 | 0 | 0/0 | 0 | 0 / 0 |

A 32,768-token window with masking on (`--context 32768`):

| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |
|---|---|---:|---:|---:|---:|---:|---:|
| test-fix loop (202 turns) | baseline | 6,467,314 | 1 | 26 | 25/26 (96%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 6,467,314 | 1 | 26 | 25/26 (96%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 8,390,926 | 0 | 80 | 80/80 (100%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 8,390,926 | 0 | 80 | 80/80 (100%) | 0 | 0 / 0 |
| exploration (170 turns) | baseline | 6,724,925 | 2 | 40 | 38/40 (95%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 7,007,982 | 0 | 36 | 36/36 (100%) | 45,575 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 7,233,216 | 1 | 48 | 47/48 (97%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 7,007,982 | 0 | 36 | 36/36 (100%) | 45,575 | 0 / 0 |
| build failures (150 turns) | baseline | 5,028,257 | 0 | 23 | 23/23 (100%) | 0 | 0 / 0 |
| build failures (150 turns) | trim | 5,028,257 | 0 | 23 | 23/23 (100%) | 0 | 0 / 0 |
| build failures (150 turns) | config-only (0.55/0.80) | 5,028,257 | 0 | 23 | 23/23 (100%) | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 5,028,257 | 0 | 23 | 23/23 (100%) | 0 | 0 / 0 |

The same window without masking (`--context 32768 --no-mask`: chat sessions or
`read_artifact` denied by policy):

| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |
|---|---|---:|---:|---:|---:|---:|---:|
| test-fix loop (202 turns) | baseline | 9,461,308 | 5 | 92 | 87/92 (94%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 9,127,976 | 2 | 75 | 73/75 (97%) | 756,998 | 40 / 40 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 10,502,405 | 3 | 123 | 120/123 (97%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 10,255,598 | 1 | 113 | 112/113 (99%) | 708,141 | 40 / 40 |
| exploration (170 turns) | baseline | 7,906,385 | 4 | 53 | 49/53 (92%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 7,346,742 | 0 | 18 | 18/18 (100%) | 144,917 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 8,707,067 | 3 | 81 | 78/81 (96%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 7,346,742 | 0 | 18 | 18/18 (100%) | 144,917 | 0 / 0 |
| build failures (150 turns) | baseline | 7,166,762 | 3 | 67 | 64/67 (95%) | 0 | 0 / 0 |
| build failures (150 turns) | trim | 7,001,116 | 1 | 63 | 62/63 (98%) | 668,934 | 49 / 48 |
| build failures (150 turns) | config-only (0.55/0.80) | 7,894,933 | 2 | 89 | 87/89 (97%) | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 7,839,126 | 1 | 82 | 81/82 (98%) | 664,951 | 49 / 48 |

What the numbers say (2026-10-13):

- **With masking on, trim rarely has anything to do.** A failing `pytest` or
  `make` run is still a successful `run_shell` call (the exit code is in the
  text), so once used, each long log becomes a receipt before trim runs. Trim
  then only touches what masking leaves: results under `mask_min_chars`. On
  the test-fix and build transcripts it saves nothing at either window size.
  On exploration (re-read files and repeated listings, mostly under the masking
  threshold) it saves 1% of characters and halves the passes at 65,536 tokens.
  At 32,768 tokens it removes both summary calls but sends 4% *more*
  characters: a summary resets the context to a small size, so averting one
  leaves the later prompts larger.
- **Without masking, trim cuts summary calls on every transcript** (5 to 2, 4 to
  0, 3 to 1) and sends 2-7% fewer characters. That beats the config-only arm,
  which saves fewer summary calls (5 to 3, 4 to 3, 3 to 2) and sends 10-11% more
  characters because the context sits higher between passes. Trim plus the
  wider gap makes the fewest summary calls but sends more than trim alone.
- **The config-only arm** removes summary calls wherever there are any, always
  at a character cost (8-30% more with masking at 32,768 tokens).
- **"Needed again" says nothing on fixed scripts.** Nearly every trimmed
  `pytest` or `make` output recurs within 10 turns because the scripts re-run
  the command after each fix, not because the trimmed lines were needed. Only
  live sessions can show whether head-and-tail loses something; the #639
  counters (`elide_only_compactions`, `read_artifact_calls`) are where to look.

The synthetic transcripts model typical sessions; they are not a sample of
them. Fewer summary calls is what 2609.20804 measures as efficiency (fewer
lossy rewrites and model calls). The rules stay on by default: in every case
trim makes no more summary calls than the baseline and never sends more characters
than the config-only arm. Their reach is mostly sessions without masking (chat, or
`read_artifact` denied) and small repeated results.

## Summaries build on the previous one

A summary replaces the older turns with one `[Context summary]` message
right after the pinned system prompt and task. The next summary request does
not fold that message into the conversation excerpt, where it would be the
first thing cut when the excerpt is over its budget. It goes whole into a
separate **Prior notes** block ahead of the excerpt, and the prompt tells the
summarizer to update those notes rather than restate them (#640). The block
holds the summary text only: the saved `update_notes` text is appended again
by the new summary message itself. The runner keeps the last summary's text
in `run["summary"]` (and the notes it was framed with in
`run["summary_notes"]`), carried into follow-up runs like the notes, and uses
it when the message is exactly what that summary produced, so a summary that
quotes the saved-notes heading is not cut short and a rewind that restored a
different summary is not mistaken for it. Otherwise only the exact saved-notes text is cut
from the end of the message; when that doesn't match either, the whole body
goes into the block, since repeating stale notes is better than dropping
findings. The excerpt's budget (45% of the context
window) shrinks by the block's size, so the request stays within the same
total. The newer messages are removed once summarized, so the block never
takes more than half of that budget from them: a longer block keeps its start
and end and loses its middle, marked as shortened.

## Overflow compaction

Compaction normally runs before a model call, on an estimated size. When the
estimate is short and llama-server rejects the prompt (HTTP 400 with
`request (N tokens) exceeds the available context size (M tokens), try
increasing it`, the message of the pinned b10950 build; the match is
`llm.CONTEXT_OVERFLOW`), the runner runs one forced elide-and-summary pass,
whatever `summarize_at` says, and retries the call once. The pass emits a
`compaction` event like a threshold one plus `reason: "overflow"`; its `tier`
is `summary`, or `elide` when there was too little to summarize or the
summary call failed. If the retry is rejected too, the run fails as it did
before. Other HTTP 400 errors are not retried.

## Round reset

After masking, compaction may **reset** the conversation instead of eliding or
summarizing. That happens for an explicit `reset_round` tool call, or when
estimated tokens reach `compaction.reset_at` of the context window. Both paths
use the same check: a valid saved state must exist (`update_state` succeeded
and the run still holds that object). The check runs at the moment the reset
would apply, so a scheduled reset is skipped if state was cleared in between.

`reset_round` without a valid saved state returns a tool error telling the
model to call `update_state` first and does not schedule a reset. A threshold
trigger with no valid state falls through to ordinary elide/summary and does
not increment round-reset accounting. A successful reset keeps the pinned
head, injects the tagged state (including derived `files_modified`), a fixed
next-step message, and the latest tool-call exchange.

## Workspace guidance and repo map

Native (local-model) agent sessions on the tower carry the repository's own
guidance file in their system prompt (#642): `AGENTS.md` at the workspace
root, or `CLAUDE.md` when there is no non-empty `AGENTS.md`. Nested files,
`.claude/CLAUDE.md` and user-scope files are not read. The text is capped at
16,000 characters, the same cap hosted Claude uses for `CLAUDE.md`, and a file
reached through a symlink or junction is skipped. It goes after the project
`instructions` and any skills, inside a `<workspace-guidance>` section whose
first line names the file. With the experimental repo map on, the map follows
it.

Both are added once, at the session's first run after the clone, and refreshed
only by a successful round reset, so the prompt prefix stays cache-stable
within a round. An edit to `AGENTS.md` mid-round reaches the model at the next
reset. Hosted backends are unchanged: Claude gets `CLAUDE.md` through
`--append-system-prompt` and Codex reads `AGENTS.md` itself.

The file is trusted like the rest of the workspace, as the repo map is: reading
it does not taint the session. A project whose repo is untrusted should be
treated as untrusted as a whole. Because the harness's own repository has an
`AGENTS.md`, native sessions on an agent-harness project now see it.

## Plan re-injection (experiment)

`planning.reinject` (#641, off by default) tests arXiv 2609.20804 Finding 4:
the agent's saved plan, re-injected on every turn and never stored in the
history, kept the weakest model from ending runs with no edit. With the flag
on, a native agent session (backend `local`, kind `agent`) works like this:

- Every model call gets the `plan` and `next_step` from the session's saved
  `update_state` object, when that object passes `STATE_SCHEMA` and at least
  one of the two fields is non-empty. The text (`Plan:` and `Next step:`) is
  capped at 1,200 characters: a longer one keeps its start and its end, with
  `…` between them. It goes after a blank line and the line
  `[current plan, from update_state; not part of the conversation]` at the
  end of the last message's content (a `tool` result, or the user's own
  message), in a deep copy of the request (`harness/planning.py`,
  `Runner._outgoing`). It adds no message and changes no role, so Qwen3-family
  templates still render the reasoning of the assistant turns after the last
  user message. `s["context"]`, the transcript, checkpoints, compaction,
  masking and the events never contain it.
- Nothing is inserted above the newest turn, so llama-server's reused prefix
  ends only where the plan began: each call re-reads the plan plus the new
  turn, not the whole newest turn again.
- One sentence asking the model to keep its plan current with `update_state`
  is added to the system prompt when the session is created. It is fixed for
  the session and never breaks the prefix.

With the flag off, the request is the stored context object itself, byte for
byte the same as before the flag existed. Chat, App-tools-only and hosted
sessions never get the plan or the sentence; hosted CLIs keep their own plan
tools. The flag is file-only (`config/harness.yaml`) and validated at load:
`planning` must be a mapping whose only key, `reinject`, is `true` or `false`.
The fake model ignores the block when it picks a script, so fixtures behave
the same either way.

### Canary comparison (owner, on the tower)

Run this before turning the flag on by default. It needs the local model and
the GPU, so CI never runs it.

1. **Baseline, flag off: the nightly history.** With `planning.reinject`
   absent or `false`, take the latest 5 `complete` rows from the daemon's
   database (`data_dir/harness.sqlite3`, [canary-evals.md](canary-evals.md)):
   `SELECT sha, pass_rate, turns, prompt_tokens, attempts FROM canary_results
   WHERE status = 'complete' ORDER BY finished_at DESC LIMIT 5`. Their median
   pass rate is the regression rule's baseline. `turns` and `prompt_tokens`
   are run totals, so compare them per valid attempt.
2. **Same commit, both arms.** The nightly runs each commit once, so a
   flag-on night would also differ in code. Stop the daemon and, on the
   deployed commit, run `python -m bakeoff.canary` (flag off) and
   `python -m bakeoff.canary --plan-reinject` (flag on, whatever
   `harness.yaml` says), twice each, alternating. Each run takes up to
   `canary.total_cap_seconds`. Results land in `runs/canary-*.json` and
   `runs/canary-plan-*.json`: per attempt `ok`, `status`, `turns` and
   `prompt_tokens`. Count only `done`, `failed` and `wall_limit` attempts
   (`harness.canary.valid`).
3. **Compare** per arm: pass rate (passes over valid attempts), median turns
   per valid attempt and median prompt tokens per valid attempt. Check the
   same-commit flag-off arm against the nightly baseline as a sanity check.
   One task's two attempts are about 11 points of the 18-attempt set, so
   treat a smaller difference as noise unless more runs repeat it.
4. **Record** the numbers in the table below, then decide whether to turn the
   flag on by default (a separate change).

| Arm | Commit | Runs | Valid attempts | Pass rate | Median turns | Median prompt tokens | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Nightly baseline (off) | | 5 | | | | | `canary_results` |
| Same commit, off | | 2 | | | | | `runs/canary-*.json` |
| Same commit, on | | 2 | | | | | `runs/canary-plan-*.json` |

Results: not yet run.

## Delegated edits

`delegate_edit(paths, instruction, task_id?)` keeps file contents out of the
worker's context for code edits. The daemon reads the listed workspace files
itself (strict UTF-8, no line numbers; a decode failure or NUL byte is a
"binary file" error; each file at most the read cap, all of them at most half
the context window), sends them with the instruction to the session's model in
a fresh context capped at 4096 completion tokens, and parses a JSON list of
`{path, old_text, new_text}` edits. Every edit must match exactly once and
edits must not overlap. The proposal and the sha256 of each file's text are
stored on the run; the worker gets back only
`{patch_id, summary, status, error}`.

`apply_delegated_edit(patch_id)` rechecks the hashes, then writes the whole
proposal or nothing. Its policy decision is that of an `edit_file` of each of
its files (the strictest wins), and an approval shows the proposal's diff. A
stale file is an error and does not use up a delegation.

An edit (keyed by `task_id`, else the sorted file list) may be delegated three
times per run, failed calls included; the count lives in
`run["delegate_edits"]`, survives round resets and restarts, and is cleared
when a proposal for it is applied. Delegate tokens are added to session totals
(not as turns) and tallied in `delegate_tokens` on the run and in totals; they
don't count against the run's completion budget. Only local-model agent
sessions on the tower with a workspace are offered these tools.

## Tool-schema overhead

Every model call sends the session's full tool list, and those schemas take
window space that is not in the context list. Each `turn_metrics` event
records `schema_count` and `schema_tokens` (the schemas' JSON characters
divided by the session's `chars_per_token`, the same estimate compaction uses
for its thresholds); session totals add `schema_tokens` per turn, and the
session metrics API sums it in `aggregate.schema_tokens`.

Measured with `python scripts/measure_schema_overhead.py` on 2026-10-12 against
the checked-in config (no model, chars per token 3.0, 64K window). Real token
counts are usually a little lower, since JSON tokenizes at more than three
characters per token.

| Toolkit | Schemas | Est. tokens |
| --- | ---: | ---: |
| Core (`list_files` to `finish`, including `verify` and `update_notes`) | 12 | 1850 |
| `read_artifact` | 1 | 172 |
| `delegate_edit` and `apply_delegated_edit` | 2 | 510 |
| Web | 2 | 360 |
| Memory library (writes on) | 5 | 792 |
| Session search | 2 | 379 |
| Homelab | 6 | 878 |
| Images | 1 | 442 |
| Skills | 1 | 438 |
| Remote control | 1 | 226 |

| Profile | Schemas | Est. tokens | Share of 64K |
| --- | ---: | ---: | ---: |
| Chat session (web) | 2 | 360 | 0.5% |
| Hosted split mode, harness MCP tools (+ web, session search) | 10 | 1731 | 2.6% |
| Default tower agent (core, `read_artifact`, delegate pair) | 15 | 2533 | 3.9% |
| + web, session search | 19 | 3272 | 5.0% |
| + web, session search, memory (writes on) | 24 | 4065 | 6.2% |
| Every built-in toolkit | 33 | 6050 | 9.2% |

Hosted CLIs (Claude Code, Codex) add their own built-in tools and defer them
themselves; the hosted row counts only what the harness serves over MCP, and
their windows are larger than 64K. MCP client catalogues (up to 128 tools per
session) depend on the configured servers and are not in the table; a
session's `turn_metrics` show their real cost.

Deferred loading (#645 phase 2) was not built. The schemas it would defer
(`update_notes` 92 tokens, `verify` without checks 155, memory writes 395,
homelab 878) save 0.4% of the window in the default profile and at most 2.3%
with every toolkit on, before the cost of a `tool_search` schema. Local
sessions also keep the tool list in llama.cpp's cached prompt prefix, so it
costs window space but is not recomputed each turn, while adding a deferred
schema mid-run would change that prefix. Revisit if a session's
`schema_tokens` show a large MCP catalogue.

## Counters

Each `turn_metrics` row carries these increments since the previous row, and
session `totals` adds them up (#639). They measure what masking, editing and
elision cost, and they change no behaviour. `summary_averted` is set on each
`elide` compaction event; it is `true` when elide alone brought the context under
`summarize_at`, so no summary call followed.

| Field | Counts |
| --- | --- |
| `read_artifact_calls` | `read_artifact` calls that ran, failed ones included; argument-validation rejections do not count |
| `read_artifact_repeats` | successful recoveries of a digest already recovered in the session |
| `edit_mismatches` | `edit_file` calls rejected with `must appear exactly once` |
| `elide_only_compactions` | `elide` passes with `summary_averted` set |

## Research backlog

[Context Language Models](context-language-models-research.md) is a concept to investigate (2026-10-01); the note describes a proposed comparison with the current implementation.
