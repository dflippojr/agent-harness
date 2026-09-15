# Phase 7a: session search

Built 2026-09-15. Decision (user): both a search box in the phone app and agent tools.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Index | `search_index` FTS5 table in the session database (`harness/db.py`) | `porter unicode61 remove_diacritics 2` tokenizer; one row per indexed event; backups carry it |
| Indexing | `Database.insert_event` → `search.event_text` | titles, user messages, assistant text plus the tool calls it made, tool results (first 6,000 characters), final answers, app context, errors |
| Backfill | `Database._build_search_index` | runs once per index version (`meta` table), at daemon start |
| Search | `harness/search.py` `search()`, `GET /search?q=&project=&limit=` | passages mark matches with `…` |
| Agent tools | `session_search(query, project, limit)`, `session_read(session_id, start, find)` | daemon-side for every target; the calling session is left out of its own results; projects opt out with `session_search: false` |
| App | session list | search box above the list; results show status, project, age, match count and up to three highlighted passages |
| Tests | `tests/test_phase7.py` | query quoting, ranking and fallback, project filter, phrases, exclusion, backfill once, agent tools end to end, API |

## Design

- **No LLM.** Queries are turned into safe FTS5 syntax: every word is quoted (so `NEAR`, `OR`, `-` and `:` in model text
  can't break or change the query), `"phrases"` stay phrases, a trailing `*` keeps prefix matching.
- **All words first, then any word.** When no session matches every word (typical for a model's natural-language
  query such as "how did we fix the ntfy OOM last time"), the search retries with any word, stopwords dropped, and
  ranks sessions by how many of the words they match before BM25.
- **Grouped by session.** Each session is scored by its best event, with BM25 weighted by where the match is (title
  3×, answer 2×, user message 1.5×, assistant 1.2×, tool output 0.8×) and a small bonus for sessions from the last
  week.
- **`session_read`** renders a compact transcript (user messages, assistant text, tool calls, tool output cut to ~500
  characters, approvals, review, final answer) in 12,000-character pages, with `find` for passages around a regex.
  Results are labelled as background that may be outdated.
- Toolkit clean-up: every daemon-side toolkit (memory library, web, images, search) now provides its own
  `schemas()`, and kits that need the calling session say so with `wants_session`.

## Verification

- 92 tests pass (1 skipped), including 5 new ones.
- Live index on the tower's real history (19 sessions, 647 events) built at start without a noticeable delay.
  `harness-demo restart` → the two homelab restart sessions first; `stale grafana plex` → `efcb152521`.
- **Exit test** (session `93b09d8544`, Qwen, project `scratch`): "An agent once looked into the plex-webhook Grafana
  dashboard showing stale data. Using past sessions, tell me what it found, whether it fixed it, and what the right
  next step would be."
  - `session_search` found `efcb152521` first; `session_read` read it in two pages (12K characters each).
  - Answer: the last-event gauge started at 0 (hence "56.7 years"), there was no active-sessions metric, the fix
    was written but never deployed, so the next step is a rebuild. Matches the session.
  - 4 turns, 20.7K prompt tokens, 740 generated, ~18 s once the model was loaded (it had to wake first: 81 s).
