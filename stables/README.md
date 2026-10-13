# Stables

Stables is the browser client and installable PWA for Agent Harness Server. Each model is a horse; the agent system
is its harness. The user guide is [`docs/web.md`](../docs/web.md).

`public/` is the whole site: native JavaScript, no build step, served as-is from the root of an origin. Agent Harness
Server serves it at `/` when it finds it (`HARNESS_STABLES_DIR`, else this checkout's `stables/public`), and runs
without it otherwise. Any static host can serve it too; see "Use a separately hosted copy" in the guide.

Stables talks to the Server only through the documented API (`/api/v1`, `/api/admin/v1`, and `GET /health`).
`tests/test_stables_boundary.py` checks that it loads only its own files, that only its transport opens connections,
and that the Server names this directory in one place.

This directory is moving to its own repository (#334). Until then its type checks live in `tools/web-types` and its
Node test harnesses in `tests/web_*.mjs`.
