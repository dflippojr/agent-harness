# Phase 7e: documents and a recorded web

Built 2026-09-15. Scope (user): `web_fetch` reads PDFs and other non-HTML documents, and there's a reproducible local
search and page fixture for benchmarks.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| PDF | `web_tools.pdf_to_text` (pypdf, BSD) | text layer with `--- page N of M ---` markers, up to 400 pages; password-protected and scanned (no text layer) PDFs are refused with a clear reason; also detected by `%PDF-` when served as a generic type |
| Word | `web_tools.docx_to_text` (standard library) | paragraphs, `Heading N` styles as `#` headings, table rows as `a \| b`, title from document properties |
| Size cap | `web.max_document_bytes` (25 MB) | pages stay at `web.max_bytes` (5 MB) |
| Fixture | `harness/web_fixture.py` | `record` saves real SearXNG results and raw page bytes (through the same address-checked download); `web.fixture_dir` makes WebTools replay them with no network: an unrecorded query gets the closest recorded one (word overlap ≥ 0.25) or no results, an unrecorded URL gets 404, and misses are counted |
| Suite | `bakeoff/web_suite.py` | research tasks through the real Manager and runner with the always-on model; answers graded by regex, and **quoted passages must appear in something the agent fetched** |
| Data | `D:\Agents\harness\web-fixture` (not in git: third-party content) | 8 searches, 18 pages including the 2.2 MB arXiv PDF |
| Tests | `tests/test_phase7.py` | generated PDF (pages, find, octet-stream, scanned), generated .docx, unsupported types, fixture record/replay/redirect/misses |

## Suite

| Task | Needs | Graded on |
| --- | --- | --- |
| `pdf_transformer` | read the Transformer paper PDF | 8 heads, d_model 512, d_ff 2048 |
| `llama_sleep_endpoints` | find the right README section | `/health`, `/props`, `/metrics` |
| `searxng_license` | search, then a page that states the license | AGPL / Affero |

Final run (`runs/web-20260915-075030.json`, Qwen, 2 repeats): **6/6**.

| Task | Run 1 | Run 2 |
| --- | --- | --- |
| pdf_transformer | 29 s, 4 turns, 3 calls | 32 s, 5 turns, 5 calls |
| llama_sleep_endpoints | 34 s, 6 turns, 7 calls | 148 s, 14 turns, 13 calls |
| searxng_license | 15 s, 7 turns, 6 calls | 10 s, 4 turns, 3 calls |

The PDF answers quote the paper with the right pages (h = 8 on page 5, d_model = 512 on page 3, d_ff = 2048 on page 5).

## Finding: made-up citations

The license is always right, but in **3 of 7** `searxng_license` runs Qwen "quoted" the GitHub repository page ("This
project is licensed under the GNU Affero General Public License (AGPL-3.0).") after fetching it, and the fetched text
(436 characters of README intro) contains no such sentence. It answered from its own knowledge and attributed that to
the page. The suite's grounding check was added for this and fails those runs. It compares letters and digits only,
because PDF text layers break spacing and subscripts (`df f = 2048`); the first strict version wrongly failed two
genuine PDF quotes.

Not fixed in the harness: a prompt rule alone is unlikely to stop it (compare the Phase 0 memory suite). Options for
later: flag quotes in final answers that don't appear in the session's tool output, or improve extraction of GitHub
repository pages, which trafilatura cuts to the README's first lines.

## Verification

- 119 tests pass (1 skipped).
- The 6b test that expected PDFs to be refused now expects a broken PDF to be reported as unreadable.
