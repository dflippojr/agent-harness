# Phase 6a: Hermes Agent design study

2026-09-15. Source read: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) at `f9ea3a5`
(2026-09-14), MIT, shallow clone in `D:\Agents\reference\hermes-agent`. About 330K lines of Python; this study read
only the parts that bear on the next mini-phases (web tools, API server, image generation, skills, memory, session
search, cron, approvals, install). No Hermes code is copied into the harness; where a pattern is adopted, it's
reimplemented and credited in a comment.

## What Hermes is, in one paragraph

A general assistant runtime: one agent loop behind a TUI and a "gateway" that bridges Telegram, Discord, Slack,
WhatsApp, Signal, email and an OpenAI-compatible HTTP server. Around the loop it has a large plugin surface
(web search/extract vendors, image and video generation, browsers, terminals on seven backends), a "learning loop"
(curated memory files, agent-written skills, background review after turns, FTS5 search over past sessions), a cron
scheduler with delivery to any platform, subagent delegation, and profiles. It's built around hosted models and paid
tool APIs (the Nous Portal bundles them); local models work through "your own endpoint".

It is not a replacement for this harness: it has no per-session Docker sandbox with a workspace-only mount by default,
no git-branch review flow, no GPU queue, and its security model is a single trusted user on their own machine. It is
a good catalog of solved problems for the assistant half.

## Findings by mini-phase

### 6b — web search and fetch

| Hermes does | Adopt? | Notes for the harness |
| --- | --- | --- |
| Separate `web_search` and `web_extract` tools over pluggable backends; SearXNG is one backend (`/search?format=json`, results sorted by `score`, capped to `limit`) | **Yes** | Two tools: `web_search` (SearXNG) and `web_fetch`. One backend for now; keep the provider seam small. |
| SSRF guard (`tools/url_safety.py`): block private, loopback, link-local, multicast, unspecified, **CGNAT 100.64.0.0/10**, and cloud-metadata addresses; re-validate every redirect target; pin the connection to the validated IP (keeps Host/SNI) so DNS rebinding can't swap the address | **Yes, required** | The tower is on the tailnet (100.x) and the homelab (ntfy, Prometheus, Plex, Grafana, the daemon itself on 127.0.0.1). A fetch tool without this is a way into all of them. |
| Extract with no LLM: pages under a char budget (default 15,000) returned whole; bigger pages become head + tail plus a footer saying where the full text is stored and how to page the middle | **Yes** | Store full text daemon-side per session; `web_fetch` takes `start` to page. Qwen reads ~800 tok/s, so a 15K-char page is ~5 s of prefill. |
| Inline base64 images replaced with `[IMAGE: alt]` placeholders ("token bombs") | **Yes** | Cheap, and pages with embedded images otherwise waste thousands of tokens. |
| Result memoization cache | **Yes (simple)** | Short TTL per query/URL; also makes benchmark runs reproducible. |
| User-managed domain blocklist | Later | Not needed for one user; leave a config hook. |
| Keyless "rescue" fallbacks, many paid vendors | No | D3: no paid APIs. |

### 6c — inference endpoint

| Hermes does | Adopt? | Notes |
| --- | --- | --- |
| OpenAI-compatible server exposes the **agent** as a model (`/v1/chat/completions`, `/v1/responses`) plus `/v1/runs` with events, approval, steer and stop | Partly | 6c is plain inference, so chat completions proxy to llama-server. The `/v1/runs` shape (run, events stream, approve, steer, stop) is a good template for the 6e app API. |
| `/v1/capabilities` endpoint listing features and routes | **Yes (6c/6e)** | Lets clients discover what the tower supports (tool calls, Anthropic route, models) instead of hard-coding. |
| Bearer key checked with a constant-time compare; server refuses to start without a key | **Yes** | Per-device keys, stored hashed, constant-time compare. |
| Global concurrency cap across agent-serving endpoints | **Yes** | We have one GPU slot: endpoint requests jump the queue between agent turns (user decision); cap queued endpoint requests so an editor can't starve agents entirely. |
| Anthropic adapters (`agent/anthropic_adapter.py`, `anthropic_message_convert.py`) convert between Messages and chat-completions shapes, incl. tool use and thinking blocks | **Study when building** | Our Anthropic Messages route needs the same conversions (tool_use/tool_result blocks, system as a top-level field, streaming event names). |

### 6d — image generation

| Hermes does | Adopt? | Notes |
| --- | --- | --- |
| Image generation only through hosted providers (FAL, OpenAI, xAI, Krea, ...) with a provider registry | No backend to reuse | Local ComfyUI has to be written from scratch. |
| Unified tool input (prompt, aspect ratio from a fixed enum), translated per model and filtered to what that model supports | **Yes** | One `generate_image` tool; each ComfyUI workflow declares supported inputs; unknown keys dropped, not sent. |
| Upscaling strictly opt-in per call (default-on degraded text and faces) | **Yes** | Don't upscale by default. |
| Artifact upload/download endpoints on the API server | **Yes** | The phone and agents need a way to fetch generated files: store per session, serve through the daemon. |

### 6e — distributable daemon and app API

| Hermes does | Adopt? | Notes |
| --- | --- | --- |
| Windows installer (`install.ps1`) needs no admin: bundles `uv`, Python, Node, ripgrep, ffmpeg and a portable MinGit under `%LOCALAPPDATA%` | **Yes, model it** | Our installer: `uv`-managed Python under `%LOCALAPPDATA%\agent-harness`, llama.cpp CUDA zip, model download, scheduled tasks; Docker Desktop and NVIDIA driver as checked prerequisites. |
| README section on antivirus false positives for `uv.exe`, with attestation verification steps | **Yes** | Expect the same with bundled binaries; document it up front. |
| `hermes doctor` diagnostics command | **Yes** | We already have `ops/check-stack.ps1`; make it the installed `doctor`. |
| Profiles (multiple isolated homes, `/p/<profile>/` routing) | Not now | One user per install for this iteration. |
| `COMPAT_MANIFEST.md` + CI check that keeps old plugin import paths working for a scheduled window | **Yes (lightweight)** | Version the app API (`/api/v1`) and keep a changelog of breaking changes. |
| Skills in the open agentskills.io format (`SKILL.md` with YAML frontmatter: name, description, platforms, prerequisites) | **Consider** | A reasonable format for app-registered instructions/context packs in 6e. |
| Skills install policy by trust tier, with a static scanner for dangerous patterns | Later | Only matters once third parties ship skills or tools to a daemon. |

## Assistant features not in a mini-phase (candidates for later)

- **Frozen memory snapshot.** Memory files enter the system prompt as a snapshot at session start; writes during the
  session go to disk but don't change the prompt, so the prefix cache survives. Directly relevant to Qwen's slow
  prefill: anything injected into the system prompt must stay byte-identical across turns.
- **Memory writes scanned for injection/exfiltration patterns**, and refused if the file drifted on disk
  (prevents silent loss when something else edited it). Matches our earlier finding that sensitive-write gates must
  be enforced by the daemon, not instructions.
- **Session search:** FTS5 over past sessions, no LLM, modes for discover/scroll/read/browse, cron sessions demoted so
  they don't drown the user's own. Cheap to add to our SQLite events table; likely more useful than agent-written
  memory for "what did we do last time".
- **Agent-written skills + background review.** After a turn Hermes forks the agent to ask "should a skill or memory
  be saved?". With one GPU slot and ~70 s cold prefill this costs real time; if ever adopted, run it only when the
  queue is idle, and route writes through an approval.
- **Smart approvals:** an auxiliary LLM rates shell commands APPROVE / DENY / ESCALATE, with comments stripped and the
  command wrapped in delimiters because the command text is untrusted. Could cut phone approvals, but needs a second
  model loaded; revisit if approval volume becomes annoying.
- **Cron:** jobs with a schedule plus a prompt, script, or skills, and a delivery target; prompts scanned for
  injection at create time. A natural harness feature (e.g. "every morning, check the homelab and notify me");
  suggest as a future mini-phase.

## What this changes in the plan

- 6b gets an explicit SSRF requirement (private, loopback, link-local, **tailnet CGNAT** and metadata ranges; redirect
  re-validation; IP pinning), head+tail paging instead of summarization, and base64 stripping.
- 6c adds `/v1/capabilities` and a queued-request cap; the Anthropic route gets its own conversion layer and tests.
- 6d adds per-workflow supported-input filtering, opt-in upscaling, and an artifact store served by the daemon.
- 6e uses a Hermes-style no-admin installer, a `doctor` command, an AV false-positive note, and a versioned API with
  a changelog; `/v1/runs`-style endpoints shape the session API.
- New candidates after Phase 6: session search, frozen memory snapshot for user context, cron jobs.
