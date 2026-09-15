# Phase 6b: web search and fetch

Built 2026-09-15. Decision (user): search and fetch run without approval; the sandbox stays offline.

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| SearXNG | `D:\Docker\searxng` (private repo `searxng-homelab`), image `searxng/searxng:2026.9.15-94218a3ac` | `127.0.0.1:8888`, JSON format on, limiter off, secret in `secrets/searxng.env` (untracked) |
| Tools | `harness/web_tools.py`, `web:` in `config/harness.yaml` | `web_search(query, limit)`, `web_fetch(url, start, find)`; daemon-side for every target |
| Wiring | `harness/runner.py` (`daemon_toolkits`), `harness/manager.py` (`WEB_PROMPT`) | projects opt out with `web: false` |
| Check | `ops/check-stack.ps1` | SearXNG answers a test query |
| Tests | `tests/test_phase6.py` | 20 tests (address policy table, pinning and redirect re-check, names/schemes/mixed DNS, extraction, paging, find, base64, size cap, search formatting and cache, session wiring and opt-out) |

## Design

- **Search:** SearXNG JSON, sorted by score, de-duplicated by URL, snippets cut to 300 characters, cached 30 min.
- **Fetch:** `httpx` with `trust_env=False`, no cookies, 20 s timeout, 5 MB cap. HTML goes through trafilatura
  (Markdown output, main content, tables kept; falls back to all visible text when extraction finds under 200
  characters). Text-like types are passed through; anything else (PDF, images) is refused. Inline base64 images become
  `[IMAGE: alt]`. Full text cached per URL for 30 min.
- **Paging:** 15,000 characters per call, cut at a paragraph or line break when one is near, with a footer giving
  the next `start`.
- **`find`:** a case-insensitive regex returns up to 12 merged passages (±400 characters) with their character
  offsets. Added after the first live run, where Qwen paged through a 60K-character README four times looking for
  one phrase.
- **Address policy (required by the 6a study):** refuses loopback, private, CGNAT/tailnet (100.64.0.0/10),
  link-local/metadata, multicast, reserved and non-global addresses, including IPv4-mapped IPv6; refuses
  `localhost`, `.local`, `.lan`, `.home`, `.internal`, `.ts.net` names and URLs with credentials; refuses a host if
  **any** resolved address is non-public. Redirects are followed by hand (max 5), each hop checked. The connection
  goes to the checked IP with the original `Host` header and TLS SNI (httpcore's `sni_hostname` extension), so
  certificate verification still applies to the real name and DNS can't change between check and connect.
- Every result starts with an untrusted-content banner, and the system prompt tells the agent not to follow
  instructions from pages.

## Verification

Live, from the tower (2026-09-15):

- HTTPS fetches to pinned IPs verified certificates (GitHub, docs.python.org), and http→https redirects were
  re-checked.
- Refused: `127.0.0.1`, `localhost`, `192.168.1.1`, `100.100.100.100`, `169.254.169.254`, `http://dflippotower/`
  (resolves to link-local), `[::1]`, `file://`.

Exit test, a Qwen session with the question "which endpoints does llama-server's `--sleep-idle-seconds` exempt, and
is there an issue about one that isn't?":

| Run | Session | Time | Turns | Final context | Tool calls |
| --- | --- | --- | --- | --- | --- |
| Before `find` | `298f47c22e` | 160 s | 11 | ~31K tokens | 4 searches, 7 fetches (README paged 4×) |
| With `find` | `4a4858237b` | 35 s | 4 | 9.7K tokens | 2 searches, 4 fetches (one `find: "exempt"`) |

Both answers cited the README and issue #18770. The second reflected the current README, which now lists `/models` and
`/metrics` as exempt too. Search took 0.5–1 s; fetches took 0.3–3 s.

## Notes

- SearXNG logs that the Tor-only engines (ahmia, torch) fail to load, which is expected without Tor. DuckDuckGo
  sometimes answers with a CAPTCHA and is skipped for that query.
- trafilatura needs `lxml_html_clean` with current lxml; both are in `requirements.txt`.
