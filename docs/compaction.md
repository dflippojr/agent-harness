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
| `strip_noise` | Removes ANSI escape sequences, keeps only what a terminal would show of a line overwritten with carriage returns, and keeps the last line of a run of progress-bar lines (a percentage plus a bar of `#`, `=` or block characters). |
| `head_tail` | `run_shell`, `verify` and `git_clone` output longer than `head_lines` + `tail_lines` + 1 lines keeps its first `head_lines` (40) and last `tail_lines` (60) lines around a marker line. File reads are left alone. |
| `superseded_reads` | Drops a `read_file` result when the same file is read again later with the same arguments, or written or edited later, by a call whose result event says it succeeded (a failed reread keeps the older contents, and so does a call with no recorded outcome, as in a fork). Paths compare as the file tools read them: only the `/workspace` prefix is dropped. The older read is dropped, not archived (gap report Section 4.3: recoverable elided content was almost never recalled); a short note names the path. |
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
| test-fix loop (202 turns) | baseline | 16,854,853 | 0 | 40 | 40/40 (100%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 16,732,715 | 0 | 37 | 37/37 (100%) | 229,919 | 20 / 20 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 16,854,853 | 0 | 40 | 40/40 (100%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 16,732,715 | 0 | 37 | 37/37 (100%) | 229,919 | 20 / 20 |
| exploration (170 turns) | baseline | 9,669,560 | 0 | 2 | 2/2 (100%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 9,566,513 | 0 | 1 | 1/1 (100%) | 34,349 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 9,669,560 | 0 | 2 | 2/2 (100%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 9,566,513 | 0 | 1 | 1/1 (100%) | 34,349 | 0 / 0 |
| build failures (150 turns) | baseline | 12,385,280 | 0 | 28 | 28/28 (100%) | 0 | 0 / 0 |
| build failures (150 turns) | trim | 11,810,737 | 0 | 16 | 16/16 (100%) | 320,563 | 24 / 24 |
| build failures (150 turns) | config-only (0.55/0.80) | 12,385,280 | 0 | 28 | 28/28 (100%) | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 11,810,737 | 0 | 16 | 16/16 (100%) | 320,563 | 24 / 24 |

A 32,768-token window with masking on (`--context 32768`):

| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |
|---|---|---:|---:|---:|---:|---:|---:|
| test-fix loop (202 turns) | baseline | 8,322,279 | 2 | 55 | 53/55 (96%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 8,597,441 | 2 | 56 | 54/56 (96%) | 394,184 | 35 / 35 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 9,540,702 | 1 | 100 | 99/100 (99%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 9,560,260 | 1 | 93 | 92/93 (98%) | 404,514 | 36 / 36 |
| exploration (170 turns) | baseline | 6,706,625 | 2 | 40 | 38/40 (95%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 6,984,015 | 0 | 36 | 36/36 (100%) | 45,575 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 7,213,044 | 1 | 48 | 47/48 (97%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 6,984,015 | 0 | 36 | 36/36 (100%) | 45,575 | 0 / 0 |
| build failures (150 turns) | baseline | 6,756,250 | 2 | 58 | 56/58 (96%) | 0 | 0 / 0 |
| build failures (150 turns) | trim | 6,878,579 | 2 | 62 | 60/62 (96%) | 474,558 | 43 / 43 |
| build failures (150 turns) | config-only (0.55/0.80) | 7,453,455 | 1 | 78 | 77/78 (98%) | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 7,358,500 | 1 | 70 | 69/70 (98%) | 481,684 | 45 / 44 |

The same window without masking (`--context 32768 --no-mask`: chat sessions,
hosted backends, or `read_artifact` denied by policy):

| Transcript | Arm | Chars sent | Summary calls | Passes | Elide-only | Trim saved | Head-tail / needed again |
|---|---|---:|---:|---:|---:|---:|---:|
| test-fix loop (202 turns) | baseline | 9,409,868 | 5 | 92 | 87/92 (94%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim | 9,129,655 | 2 | 75 | 73/75 (97%) | 755,078 | 40 / 40 |
| test-fix loop (202 turns) | config-only (0.55/0.80) | 10,547,747 | 3 | 123 | 120/123 (97%) | 0 | 0 / 0 |
| test-fix loop (202 turns) | trim + 0.55/0.80 | 10,231,394 | 1 | 112 | 111/112 (99%) | 706,221 | 40 / 40 |
| exploration (170 turns) | baseline | 7,912,620 | 4 | 53 | 49/53 (92%) | 0 | 0 / 0 |
| exploration (170 turns) | trim | 7,317,255 | 0 | 18 | 18/18 (100%) | 146,237 | 0 / 0 |
| exploration (170 turns) | config-only (0.55/0.80) | 8,841,037 | 3 | 84 | 81/84 (96%) | 0 | 0 / 0 |
| exploration (170 turns) | trim + 0.55/0.80 | 7,317,255 | 0 | 18 | 18/18 (100%) | 146,237 | 0 / 0 |
| build failures (150 turns) | baseline | 7,164,662 | 3 | 67 | 64/67 (95%) | 0 | 0 / 0 |
| build failures (150 turns) | trim | 6,974,228 | 1 | 63 | 62/63 (98%) | 677,997 | 44 / 43 |
| build failures (150 turns) | config-only (0.55/0.80) | 7,892,905 | 2 | 89 | 87/89 (97%) | 0 | 0 / 0 |
| build failures (150 turns) | trim + 0.55/0.80 | 7,828,033 | 1 | 82 | 81/82 (98%) | 671,883 | 45 / 44 |

What the numbers say (2026-10-13):

- **At 65,536 tokens with masking, nothing reaches a summary in any arm.**
  Masking and elide already hold these transcripts under `summarize_at`. Trim
  sends 1-5% fewer characters and cuts compaction passes (28 to 16 on build
  failures). The config-only arm changes nothing because no summary was due.
- **Without masking, trim cuts summary calls on every transcript** (5 to 2, 4 to
  0, 3 to 1) and sends 3-8% fewer characters. That beats the config-only arm,
  which saves fewer summary calls (5 to 3, 4 to 3, 3 to 2) and sends 10-12% more
  characters, because the context sits higher between passes. Trim plus the
  wider gap makes the fewest summary calls but sends more than trim alone.
- **With masking at 32,768 tokens the result is mixed.** Trim removes both
  summaries on exploration but leaves the other two transcripts at 2, and it
  sends 2-4% *more* characters than the baseline. A summary resets the context
  to a small size, so averting or delaying one leaves the later prompts larger.
  The config-only arm halves summary calls there at a 7-15% character cost.
  Fewer summary calls is what 2609.20804 measures as efficiency (fewer lossy
  rewrites and model calls), but prompt characters do not always fall with it.
- **"Needed again" says nothing on fixed scripts.** Nearly every trimmed
  `pytest` or `make` output recurs within 10 turns because the scripts re-run
  the command after each fix, not because the trimmed lines were needed. Only
  live sessions can show whether head-and-tail loses something; the #639
  counters (`elide_only_compactions`, `read_artifact_calls`) are where to look.

The synthetic transcripts model typical sessions; they are not a sample of
them. The rules stay on by default: in every case trim makes no more summary
calls than the baseline and sends fewer characters than the config-only arm.

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
