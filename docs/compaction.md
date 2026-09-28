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

