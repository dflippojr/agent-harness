# Phase 8c: quote checks and GitHub pages

Issues [#8](https://github.com/dflippojr/agent-harness/issues/8) and [#9](https://github.com/dflippojr/agent-harness/issues/9).

## Quotes in final answers (`harness/grounding.py`)

In the Phase 7e web suite, Qwen sometimes gave the right answer with a made-up quote attributed to a page it had
fetched. The suite caught it, but live sessions didn't. Now every session checks its final answer:

- A quoted passage of 25+ characters (straight or curly quotes) must appear in something the agent read: any tool
  output, the task, a user message, or app context. Matching uses letters and digits only, because PDF text layers
  break spacing and models add Markdown inside quotes. A quote with an ellipsis needs every part to appear.
- **First failure:** the answer isn't accepted. The model is asked once to copy the exact wording, paraphrase
  without quotation marks, or remove the quote (a user message after a plain answer, or the `finish` tool's
  result). The app shows a collapsed "Asked the agent to fix N quotes" note.
- **Still unsupported after that:** the answer is accepted and flagged. That means an `ungrounded_quotes` event,
  `run.ungrounded_quotes`, `ungrounded_quotes` in `run_finished` (so apps see it), a ⚠ warning under the answer in
  the web app, and "(check quotes)" on the Done notification.
- The request text repeats the quotes, so it's excluded from the sources. Otherwise a flagged quote would count as
  grounded by the harness's own message.
- `web.quote_check: false` turns it off. The web suite uses the same function.

Known limit: tool results in the event log are stored middle-truncated at 20,000 characters. A quote from the
elided middle of a very long output that compaction has since removed from context could be flagged wrongly.
`web_fetch` pages are 15,000 characters, so web research isn't affected.

## GitHub pages (`web_tools.github_sources` / `github_text`)

Trafilatura extracts github.com pages poorly. `web_fetch` now reads them another way:

| URL | Read from |
| --- | --- |
| `github.com/<owner>/<repo>` | API: description, license, language, stars, forks, open issues, topics, default branch, homepage, dates, archived/fork; the root file list; the README (decoded) |
| `github.com/<owner>/<repo>/tree/<ref>/<path>` | the same, with that folder's file list and README |
| `github.com/<owner>/<repo>/blob/<ref>/<path>` | `raw.githubusercontent.com` |
| issues, pulls, releases, org pages, ... | HTML as before |

The same address checks apply (every API/raw request goes through `_download`). If the metadata or raw request
fails, for example because of the unauthenticated API rate limit (60 requests an hour), the HTML page is used
instead. The web fixture recorder also stores the API and raw URLs behind GitHub pages. The tower's fixture was
re-recorded for three GitHub URLs, with a backup at `D:\Agents\harness\web-fixture.bak-phase7`.

Live check before the fixture change: `github.com/searxng/searxng` gives the license line "GNU Affero General
Public License v3.0 (AGPL-3.0)" and the README; a `tree/master/tools/server` folder in llama.cpp and a `blob` file
also read cleanly.

## Exit test

`python -m bakeoff.web_suite run --repeats 2` with Qwen on the recorded web: **6/6 passed**, and no answer had an
ungrounded quote (`runs/web-20260915-123606.json`).

| Task | Phase 7e | Phase 8c |
| --- | --- | --- |
| searxng_license | ~35 s, 4 turns | 12.1 s / 3 turns, 10.1 s / 3 turns |
| pdf_transformer | pass | 121.9 s (model wake) / 31.2 s, 4 turns |
| llama_sleep_endpoints | pass | 32.1 s / 7 turns, 44.3 s / 9 turns |

Both searxng_license answers quote "License: GNU Affero General Public License v3.0 (AGPL-3.0)" from the API-based
page. The three fixture misses in one llama_sleep_endpoints run were URLs the model guessed (a fossies mirror and
other branch names for the raw README) that were never recorded.

Tests: 126 pass (`tests/test_phase8.py` adds 7).
