# Context compaction and observation masking

Local-model agent sessions keep the model's context under the window with three
steps, in order: **mask** used tool results, **elide** old tool output, then
**summarize** older turns. This page is the operator description of masking.
Elide and summarize thresholds (`elide_at`, `summarize_at`, `keep_recent`) are
live admin keys; see [`config-registry.md`](config-registry.md).

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

## Research backlog

[Context Language Models](context-language-models-research.md) is a concept to investigate (2026-10-01); the note describes a proposed comparison with the current implementation.
