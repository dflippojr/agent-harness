# Harness-design gap report: arXiv 2609.00006 and 2609.20804 (#636)

Status: design and research report, written 2026-10-12 for issue #636. Docs only; no runtime behaviour changes.
No model was run for this report: every number about the harness comes from code or from results already
committed under `docs/`. File references are `path:line` at commit `01d9397` (the base of this branch) and name
the function or class so they survive line drift.

## Sources and how they were read

| Paper | What was read | Caveats |
| --- | --- | --- |
| **arXiv:2609.00006v1**, Barbaste, Darrigol, Vu, Wiltberger, "Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents. A Source-Code Study of Eleven Systems" | Full text. Sections 1 to 15 from the arXiv HTML rendering; Section 16 (the 18 recommendations and the 90-line scaffold, Listing 3) from the PDF, extracted locally with `pdftotext`, because the HTML fetch was cut off in Section 15.5. | The listing was confirmed on 2026-10-12: the abstract page shows one version, v1, submitted 15 July 2026, under a September-series identifier. The paper calls itself the second, expanded edition of an April 2026 study. Cited below as "P1 §n". |
| **arXiv:2609.20804v1**, Fan, Zhang, Ma, Hu, Wang, Song, Liu, Zamani, Wang, "An Empirical Study of Harness Design for Coding Agents" | Full text from the arXiv HTML rendering, including the appendix tables (Tables 10 to 13) and prompt figures (Figures 18 to 31). | Submitted 17 September 2026, one version. Cited below as "P2 §n". |

P1 is descriptive: eleven production harnesses (Claude Code, Codex CLI, Gemini CLI, Mistral Vibe, OpenHands, Aider,
Mini-SWE-Agent, Hermes, Pi, OpenCode, OpenClaw; P1 §4.1), seven subsystems (P1 §2.3, Table 1), 29 patterns (P1
§13.1, Tables 11 and 12), 18 recommendations (P1 §16) and a scaffold (P1 §16.10, Listing 3). P2 is experimental: one
fixed ReAct loop, 176 settings over four models and two benchmarks, varying planning, action space and context
management (P2 §2, §3.1). The two disagree in places (P1 Rec. 3 says start with bash only; P2 Finding 5 says that
only pays off for bash-capable models), and the report says so where it matters.

## 1. Subsystem map

The seven subsystems are P1 §2.3, Table 1. Patterns are named as in P1 Tables 11 and 12 (§13.1); the section in
brackets is where P1 dissects that subsystem. "Differs on purpose" means the harness made a different choice for a
reason it documents.

| Subsystem (P1 §) | P1 pattern(s) | What the harness does | Verdict |
| --- | --- | --- | --- |
| **Agent loop** (§6) | Iterative action-observation loop (§6.2); Stuck Detection; Outer Verification Loop; Reflection Loop; Middleware Pipeline | Hand-rolled asyncio loop `Runner._loop` (`harness/runner.py:1166`): resolve pending tool calls, drain inbox, `_budget_reason` (`harness/runner.py:1222`), `_maybe_compact` (`harness/runner.py:3181`), `_generate` (`harness/runner.py:2171`). Stops: `max_turns` 80 and `max_completion_tokens` 200000 (`harness/config.py:567-568`); `max_run_seconds` 3600 for member and App runs only (`Runner._time_left`, `harness/runner.py:725`; an owner-started run has no wall-clock cap); three idle replies (`harness/runner.py:2294`), expired approval, cancel, disk quota (`_over_quota`, `harness/runner.py:2485`). Model-call retries: `_call_with_retries` (`harness/runner.py:2212`), four attempts. Tool calls run one at a time (`_resolve_calls`, `harness/runner.py:2321`; `docs/phase1-results.md:88`). Re-entrant: each step commits before the next, restart resumes (`Manager.start`, `harness/manager.py:305`; `docs/phase1-results.md:11`). Per-turn workspace checkpoints with rewind and fork (`harness/checkpoints.py:36-99`, `Manager.rewind` `harness/manager.py:1061`). | **Matches** the linear-loop pattern with cheap caps. **Gap**: no identical-call or repeated-failure cap (`efficiency.RetryTracker`, `harness/efficiency.py:133`, only counts); no outer verification (the `verify` tool is model-initiated, `harness/verify.py:212`; the only automatic end check is `_quote_check`, `harness/runner.py:3288`); no post-edit lint or diagnostics. Inline policies instead of a middleware pipeline: see Rec. 1. |
| **LLM integration** (§7) | Provider strategies (§7.1); Prompt Caching; Model-Family Prompt Matrix; Cache-Dialect Fanout | Native sessions: one streaming client for llama-server's OpenAI-compatible endpoint, `llm.chat` (`harness/llm.py:110`), no LiteLLM or framework. Hosted sessions: vendor CLIs in containers, `ClaudeSession`, `CodexSession`, `CursorSession`, `PrintModeSession` (`harness/cli_backends.py:119, 343, 694, 953`). Prompt: `SYSTEM_PROMPT` (`harness/runner.py:62`) plus module, web, toolkit, project `instructions` and skills text (`Manager._system_prompt` `harness/manager.py:880`, `_finish_system` `harness/manager.py:713`), frozen as `context[0]` at creation (`harness/manager.py:513`). Cache: llama-server `prompt_progress.cache` recorded as `Completion.cache_tokens` (`harness/llm.py:45-51`), metric `harness_prompt_cache_tokens_total` (`harness/metrics.py:167`). Reasoning: `reasoning_content` streamed and stored (`harness/llm.py:71`, `harness/runner.py:2259`), thinking off for summaries and delegate calls (`harness/runner.py:3271, 2931`); `effort` is hosted-only and passed only to Codex (`harness/cli_backends.py:555`). | **Differs on purpose.** The harness is a single-provider native loop (local llama-server) that also *hosts* other harnesses as backends, the role P1 §14.4 describes for Omnigent and P1 §11 for OpenHands' ACP backends. It therefore does not need a provider matrix. Caching is measured, not engineered: no prompt hashing, no cache breakpoints, and the system prompt can change mid-session (`{base_branch}` fill-in `harness/runner.py:2544`, repo map `harness/runner.py:3127`), which invalidates the prefix. |
| **Tools & actions** (§8) | Four-tool surface (§8.1); Deferred Loading; Polymorphic Edits; file-editing strategies (§8.4, Table 7); sandboxing (§8.5, Table 8) | Twelve core tools in `tool_schemas` (`harness/tools.py:34-113`): `list_files`, `read_file`, `search`, `write_file`, `edit_file`, `run_shell`, `git_clone`, `update_state`, `reset_round`, `update_notes`, `finish`, `verify`; plus `read_artifact`, `delegate_edit`, `apply_delegated_edit` (`Runner.tool_schemas`, `harness/runner.py:444-458`) and toolkits (web 2, memory 5, session search 2, homelab 6, images, skills, remote control, MCP up to 128). `edit_file` is exact unique match: `old_text must appear exactly once` (`harness/fileops.py:511`); no fuzzy cascade, no read-before-write check, no post-edit diagnostics. Output caps: `read_file` 400 lines (max 2000), `search` 100 matches, `run_shell` 20000 chars (max 100000) (`harness/config.py:469-477`); over-cap shell output is stored whole as an artifact (`harness/runner.py:2825-2834`). All schemas are sent on every call (`harness/runner.py:2192`). | **Matches** P1's "bash plus file tools" shape and P2's predefined set (P2 §2.2, Table 1), minus P2's read-before-write and post-edit diagnostics. **Gap** (Rec. 4): no deferred loading, and with toolkits on the count passes P1's 15-tool line (the bake-off measured 15 schemas on `repo_qa`, `docs/harness-bakeoff-study.md`). **Gap** (Rec. 5): exact-match editing on a weaker local model, mitigated by `delegate_edit`, with no measurement of mismatch failures. |
| **Memory & context** (§9) | Threshold compaction (§9.5); LLM Summarization; JIT Repo Context; persistent memory (§9.6); Lineage Compaction; Turn-Level Checkpoint | Three tiers before every model call, `_maybe_compact` (`harness/runner.py:3181`): mask used results into receipts recoverable with `read_artifact` (`compaction.mask_used_results`, `harness/compaction.py:75`), optional round reset from saved state (`harness/compaction.py:3204`, `apply_round_reset` `harness/compaction.py:207`), `elide` old reasoning and long tool output (`harness/compaction.py:109`), then one summarisation call (`split_for_summary` `harness/compaction.py:128`, `summary_request` `harness/compaction.py:182`, `apply_summary` `harness/compaction.py:191`, `_summarize_context` `harness/runner.py:3247`). Thresholds `elide_at` 0.55, `reset_at` 0.60, `summarize_at` 0.65, `keep_recent` 0.20 (`harness/config.py:576-579`). Cross-session memory: the memory library module (regex search, approved writes, `harness_modules/memory_library/service.py:279, 363`) and FTS5 session search (`harness/db.py:316`). Repo map: tree-sitter plus PageRank, off by default, "pending run" (`harness/repomap.py`, `docs/repo-map-study.md:3`). No vector store anywhere. | **Matches** threshold compaction with a verbatim tail and matches P2's staged tier T4 (Section 4). **Gap**: no hierarchical `AGENTS.md`/`CLAUDE.md` discovery for native sessions (only hosted Claude gets `CLAUDE.md`, `cli_backends.project_memory` `docs/repo-map-study.md:50`); the previous summary is the first thing dropped when a summary excerpt overflows (`render_excerpt` `harness/compaction.py:172`); no reactive compaction when the server rejects an oversized prompt (`LLMError` non-retryable, `harness/llm.py:130`, fails the run). |
| **Safety & permissions** (§10) | Policy-as-Code; Syntax-Aware Command Permissioning; Untrusted-Content Delimiting; layered approval (§10.1, §10.2) | First-match YAML rules with allow/ask/deny, path globs, args regex, workspace confinement (`Policy.decide`, `harness/policy.py:282`; `DEFAULT_RULES` `harness/policy.py:28`, `ALWAYS_ASK` `harness/policy.py:65`). Approvals are events with a 24 h expiry and ntfy action links (`_authorize` `harness/runner.py:2622`, `_wait_approval` `harness/runner.py:2752`). Session taint escalates allow to ask after web, foreign MCP or app content (`harness/taint.py:47-63`). Smart approvals: a hosted LLM reviewer behind a closed argv allowlist, fail-safe to escalate (`assess_eligibility` `harness/smart_approvals.py:727`, `SmartReviewer` `harness/smart_approvals.py:894`). Per-session Docker sandbox, internal network, 2 GB/2 CPU/512 pids, `no-new-privileges` (`Sandbox.ensure_running`, `harness/sandbox.py:226`); `network=true` attaches an egress network per command after approval (`harness/sandbox.py:361-372`). Hosted CLIs: per-provider tinyproxy allowlists (`ops/egress/`). Web results carry an `UNTRUSTED` banner (`harness/web_tools.py:51`). Operation audit (`harness/operation_audit.py:16`). | **Matches** P1's Codex-style layering (rules, LLM reviewer, OS-level isolation; P1 §10.2) and Rec. 10 and 11. **Differs on purpose**: no PLAN/DEFAULT/YOLO session modes (Rec. 9); the owner approves from a phone and the floor (`ALWAYS_ASK`, taint) cannot be switched off. Command parsing is `shlex` plus regex, not tree-sitter. **Gap**: MCP and `session_read` results taint the session but are not delimited the way web results are. |
| **Orchestration** (§11) | Coordinator-Worker; Recursive Composition; Context Forking; ACP/A2A (§13.3) | No sub-agents. `delegate_edit` is one bounded fresh-context model call (`harness/delegate_edit.py:21-56`). Session fork from a checkpoint (`Manager.fork`, `harness/manager.py:1105`), compare runs across backends (`create_compare`, `harness/manager.py:1433`), cron jobs (`harness_modules/jobs/service.py:156`), MacBook runner (`harness_modules/runners`). Hosted CLIs keep their own sub-agents only where safe (`Task` in Claude split mode, `harness/cli_backends.py:38`; Codex `multi_agent` off, `harness/cli_backends.py:328`). No ACP or A2A. GPU admission: one session holds the slot per run (`harness/scheduler.py:18`). | **Differs on purpose** and agrees with Rec. 12 (stay single-agent) and P1 §15 (15x token cost). ACP (Rec. 13) is not met; the App API and SDK are the integration surface instead. |
| **Extensibility** (§12) | Skills; Conditional Activation; Deferred Loading; MCP; hooks; Client/Server Harness; Minimal-Core/Extension-Host | Module interface with routes, tool gates, settings, prompts (`Module`, `harness/modules.py:166`; 14 modules under `harness_modules/`). Skills are instruction-only `SKILL.md` bundles, owner-installed by hash, frozen per session and concatenated into the system prompt (`harness_modules/skills/service.py:316, 792`); no auto-activation ("V1 does not auto-select", `harness_modules/skills/service.py:232`). MCP client: owner-pinned stdio servers in network-less containers (`harness_modules/mcp_client/service.py:35-49`). MCP server for hosted CLIs with one-use grants (`harness/mcp_server.py:118`, `harness/runner.py:492-510`). Typed settings registry (`harness/settings.py`). No user lifecycle hooks or webhooks; SSE event streams only (`harness/api.py:1043`). Marketplace is design-only (`docs/marketplace-design.md:3-5`). | **Matches** Rec. 14 (skills and MCP both present, skills treated as packages with review and hashes). **Gap**: skills are neither deferred nor conditionally activated (P1 Observation 8 says 8 of 9 adopters defer); no hook surface. The client/server split is #334 and out of scope here. |

## 2. The eighteen recommendations (P1 §16)

| # | Recommendation (P1 §) | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Linear loop; graduate to a middleware pipeline once three or more orthogonal turn policies exist (§16.1) | Partly met | `Runner._loop` (`harness/runner.py:1166`) is linear, but budgets, compaction, admission, quota, approvals and checkpoints are inline branches; more than three policies. A middleware refactor touches the runner/server boundary: **after #334**. |
| 2 | Couple to your home provider; expose a generic fallback (§16.2) | Not applicable | The harness ships no model. Native loop targets llama-server only (`llm.chat`); hosted work goes through vendor CLIs, not a provider abstraction. |
| 3 | Begin with bash; add tools in response to failures (§16.3) | Met | Phase 0 ran minimal and reference harnesses first (`docs/phase0-results.md`); the twelve core tools came from those failures. P2 Finding 5 qualifies the recommendation for weaker models (Section 4.4). |
| 4 | Above 15 tools, defer tool loading (§16.3) | Not met | Full schema list on every call (`harness/runner.py:2192`); 15 schemas measured on `repo_qa` (`docs/harness-bakeoff-study.md`), more with homelab, memory and MCP toolkits. Follow-up F9. |
| 5 | Match the edit contract to the model tier: exact for frontier, fuzzy cascade for weaker (§16.4) | Partly met | Exact unique match (`harness/fileops.py:506-513`) on a 35B local model, the tier P1 says needs a cascade; `delegate_edit` (`harness/delegate_edit.py`) is the mitigation. No mismatch-rate data: follow-up F2 measures first. |
| 6 | Auto-discover hierarchical Markdown context files; read neighbours' names (§16.5) | Not met (native); partly (hosted) | Native sessions get only `projects.yaml` `instructions` (`harness/manager.py:718-720`). Hosted Claude gets `CLAUDE.md` capped at 16000 chars (`harness/cli_backends.py:46-69`); Codex gets `AGENTS.md` read-only (`harness/cli_domains.py:76`). Follow-up F5. |
| 7 | Threshold compaction below the window, verbatim tail, incremental summary merge, reactive on overflow (§16.5) | Partly met | Threshold and tail: yes (`_maybe_compact`, `keep_recent`). Merge: the earlier summary is folded into the next excerpt (`_head_len`, `harness/compaction.py:59`) but sits first and is cut first when the excerpt exceeds `0.45 * n * cpt` chars (`render_excerpt` `harness/compaction.py:172`, `harness/runner.py:3258`). Reactive: no; an oversized prompt fails the run. Follow-up F3. |
| 8 | No RAG over code; ripgrep, glob, tree-sitter, traversal (§16.5) | Met | `FileOps.search` regex scan (`harness/fileops.py:478`), `list_files`, tree-sitter repo map experiment (`harness/repomap.py`); no embeddings. |
| 9 | Developer-tool tier: three approval modes with scope patterns (§16.6) | Differs on purpose | Rules plus taint plus smart approvals replace session modes; the owner approves remotely (`docs/smart-approvals.md`). No YOLO mode exists, by design. |
| 10 | Shared/automated tier: OS-level sandbox, policy-as-code, audit trails (§16.6) | Met | Per-session Docker sandbox (`harness/sandbox.py:226`), YAML policy (`harness/policy.py:28`), `operation_audit` (`harness/operation_audit.py:16`). Minor: no `--user` or read-only rootfs is passed (`harness/sandbox.py:268-279`). |
| 11 | Rules as data; keep a floor beneath any bypass (§16.6) | Met | `ALWAYS_ASK` (`harness/policy.py:65`) and taint escalation are not project-configurable; projects cannot set `smart_eligible` (`harness/policy.py:272-273`). |
| 12 | Stay single-agent until a breadth-first phase proves parallelism (§16.7) | Met | No sub-agents; `delegate_edit` is bounded. |
| 13 | Ship an ACP server; keep sub-agents in-process (§16.7) | Not met | No ACP. The harness is itself a host for vendor CLIs and exposes the App API and SDK (`docs/app-api.md`, `sdk/`). Revisit after #334 if an IDE or meta-harness host is wanted; not filed. |
| 14 | Skills for capability templates, MCP for integrations; treat skills as packages (§16.8) | Met | Skills module with review and hash install (`harness_modules/skills`); owner-pinned MCP (`docs/mcp-client.md`). |
| 15 | No agent frameworks in the runtime (§16.9) | Met | `requirements.txt` has httpx and no framework; the loop is `asyncio`. |
| 16 | No vector-embedding retrieval for code (§16.9) | Met | None found; session recall is FTS5 (`harness/db.py:316`), the "production ceiling" P1 Observation 5 names. |
| 17 | Do not wrap every SaaS API as a 1-to-1 tool (§16.9) | Met | Homelab is six consolidated tools (`harness_modules/homelab/service.py:21`); web is two. |
| 18 | Do not over-engineer stuck detection, but ship the cheap caps (§16.9) | Partly met | Turn, completion-token and idle caps exist (`harness/config.py:567-568`, `harness/runner.py:2294`); the wall-clock cap applies to member and App runs only (`Runner._time_left`, `harness/runner.py:725`). No cap on identical calls or repeated failures; `RetryTracker` (`harness/efficiency.py:133`) observes only. Follow-up F6. |

Tally: 9 met, 4 partly met, 3 not met (Rec. 4, 6, 13), 1 not applicable, 1 differs on purpose.

## 3. The 90-line scaffold against the harness's minimal path

P1 §16.10, Listing 3, is a Python scaffold that P1 Observation 13 says implements 10 of the 18 recommendations.
The harness's minimal path is a native local session with no toolkits enabled: `Manager` creates the session,
`Runner._loop` drives it.

| Scaffold element (Listing 3 lines) | Harness equivalent | What the harness adds | Earns its keep? |
| --- | --- | --- | --- |
| `Model` protocol, one `complete()` (7-8) | `llm.chat` (`harness/llm.py:110`): streaming, usage, llama-server timings, cache tokens, retryable errors | Streaming deltas to the UI (`_DeltaStream`, `harness/runner.py:212`), per-turn metrics, four retries on 5xx | Yes. The phone UI needs streaming; the 500-on-bad-tool-syntax retry (`harness/llm.py:130`) is a measured llama-server failure mode. |
| `_truncate` at 25,000 chars (11) | `truncate_middle` (`harness/fileops.py:42`), per-tool caps (`harness/config.py:469-477`), 35% per-turn budget (`harness/runner.py:2327`), over-cap output stored as an artifact (`harness/runner.py:2825`) | Middle truncation keeps both ends; one shared budget prevents a single turn overflowing the window | Yes. P2 truncates tool results at 24k chars (P2 §3.1), the same order of magnitude. |
| Four tools: bash, read_file, write_file, search_replace (13-30) | Same four as `run_shell`, `read_file`, `write_file`, `edit_file`, plus `list_files`, `search`, `git_clone`, `update_state`, `reset_round`, `update_notes`, `finish`, `verify` | `search` and `list_files` are P1 Rec. 3's own "next step" (grep/glob when bash feels awkward); `finish` gives an explicit end; `update_state`/`reset_round` support round reset; `verify` runs configured checks | Mostly. `update_notes` is a deprecated alias and costs a schema on every call; `verify` is offered even when no checks are configured and then errors (`harness/verify.py:215`). Both are candidates for the deferred-loading follow-up F9. |
| `discover_context` walks parents for `AGENTS.md` (33-38) | None for native sessions; `projects.yaml` `instructions` only | Nothing | **The one scaffold feature the harness lacks.** Follow-up F5. |
| `Agent` dataclass: `max_turns` 50, `max_cost` 5.00, `compact_at_tokens` 120000 (42-56) | `max_turns` 80, `max_completion_tokens` 200000 (`harness/config.py:567-568`); `max_run_seconds` 3600 for member and App runs (`Runner._time_left`, `harness/runner.py:725`); fractional thresholds of the model's window | No dollar cap for native runs (local inference has no price); a wall-clock cap the scaffold lacks, but only for member and App runs: an owner-started native run is bounded by turns and tokens alone | Yes. Fractions of `context_tokens` transfer across models; the scaffold's fixed 120k would be above a 64k local window. |
| `_token_estimate`: `len(json)/4` (51-52) | `estimate_tokens` chars/cpt with `chars_per_token` calibrated per turn from real `prompt_tokens`, clamped 1.5 to 6.0 (`harness/compaction.py:47`, `harness/runner.py:2202-2206`) | Calibration | Yes. A fixed divisor drifts by model and language; the harness measured this in Phase 1 (`docs/phase1-results.md`). |
| `_maybe_compact`: one summary of the whole history, keep system plus last 30% (58-65) | Mask, reset, elide, then summarise the middle with the task pinned, pairing invariant (`split_for_summary` `harness/compaction.py:128`), `keep_recent` 0.20 | Three cheap tiers before the model call; saved-state reset; never summarising unread results | Yes: this is P2's T4 ordering (Section 4.2) and P2 Finding 2 says it is the most efficient. The scaffold's summary call also re-sends the full history as the summariser's input, which the harness avoids by rendering a bounded excerpt. |
| `run`: serial tool execution, exceptions become error strings, stop when no tool calls (67-82) | `_resolve_calls` sequential (`harness/runner.py:2321`), tool errors fed back (`harness/runner.py:2811`), final on no tool calls or `finish` (`_commit_completion` `harness/runner.py:2273`), three idle nudges | Idle nudges, `finish`, checkpoints after mutating tools (`harness/runner.py:2339`) | Yes. |
| Not in the scaffold: sandbox, policy and approvals, masking with `read_artifact`, round reset, checkpoints, events and resume, GPU scheduling, hosted backends, taint, quote check, delegate_edit, telemetry | | | Sandbox, policy, approvals, audit: required by the deployment tier (Rec. 10), earns its keep. Events and resume: required by phone control (Phase 1 and 2). Masking and `read_artifact`: see Section 4.3, keep but instrument. Round reset: a harness-specific tier with no P1 or P2 analogue; Phase 1 recorded the 16k stress case it fixed (`docs/phase1-results.md:64-77`). Checkpoints: pattern 17 (Turn-Level Checkpoint), four of eleven P1 systems. Hosted backends and GPU scheduling: the product, not scaffold overhead. Quote check: one extra pass per run, cheap. |

Net: the harness adds nothing the paper's corpus calls superfluous, and it lacks one scaffold feature (context-file
discovery). The additions to question are the always-on schemas for `update_notes` and `verify` (F9) and the
unmeasured recoverability of masked results (F2, Section 4.3).

## 4. P2 (2609.20804) findings against compaction and the tool surface

### 4.1 What P2 measured

Four models (Nemotron-3 30B, 120B, 550B; Mistral-Medium-3.5-128B), SWE-Bench Verified (500 tasks) and
Terminal-Bench 2.1 (89 tasks), 176 settings (P2 §3.1). Context tiers T0 to T4 (P2 §2.3, Table 2, Algorithm 1):
T0 none; T1 elide bulky middle observations to a stub at the hard threshold; T2 elide with an external store and a
`recall_event(id)` tool; T3 LLM summary of the oldest middle events at the hard threshold; T4 staged, elide at the
soft threshold (0.6 of the usable window) then summarise at the hard threshold (0.85), keeping a verbatim recent
window of 0.3 of the budget. Tool results were truncated at 24k characters. Planning is an `update_plan` tool
whose plan is re-injected every turn and not stored in history (P2 §2.1). The action space is a predefined set
(`read_file`, `write_file`, `edit_file`, `list_files`, `glob_files`, `grep_text`, `web_fetch`, `bash`) with
read-before-write checks and post-edit diagnostics, or `bash` only (P2 §2.2, Table 1). The planning and bash-only
ablations ran only at T4 with a 128k window (P2 §3.1, §6).

### 4.2 Does mask, elide, summarise already implement "rule-based then LLM"? Yes.

P2 Finding 2 (P2 §3.2, Figures 4 and 5): T4 matched T1 to T3 on success and had the lowest cost in 7 of 8
model-benchmark panels and the lowest peak-context ratio at every budget, because cheap elision reduced the number
of summarisation calls. The harness's `_maybe_compact` (`harness/runner.py:3181`) is the same staging:

| P2 mechanism | Harness tier | Difference |
| --- | --- | --- |
| M1 elision at the soft threshold (0.6) | `elide` (`harness/compaction.py:109`) at `elide_at` 0.55: strips old reasoning, keeps 600 chars of head and tail of tool outputs over 1500 chars outside the last 6 messages | Harness stubs keep more text than P2's one-line stub; harness also strips reasoning, which P2's models did not emit into history. |
| M2 recall store | `mask_used_results` (`harness/compaction.py:75`) plus `read_artifact` (`harness/runner.py:2981`) | Runs **every turn**, before any threshold check (`harness/runner.py:3190`), on any successful used result of at least `mask_min_chars` 2000; P2's T2/T4 elide only the middle region and only when the threshold binds. The harness is more aggressive and pays the receipt (about 200 chars) sooner. |
| M3 summary at the hard threshold (0.85) | `_summarize_context` (`harness/runner.py:3247`) at `summarize_at` 0.65 after eliding | P2's gap between soft and hard thresholds is 25 points of window; the harness's is 10. A narrow gap gives elision less room to avert a summary, which is the saving P2 attributes T4's efficiency to. |
| Verbatim recent window 0.3 | `keep_recent` 0.20 | Harness keeps less verbatim. |
| (none) | Round reset at 0.60 from saved state (`apply_round_reset`) | No P2 analogue; it replaces summarisation when the model has kept its state current. |

So #637's premise holds: the order is right, and the question is only how much more the rule tier can take before
the LLM tier runs. The committed replay evidence points the same way: on five sessions `elide()` alone saved 64.6%
of context characters and masking 70.6% (`docs/jev-compaction-study.md:64-68`), and the 16k Phase 1 stress run
needed 31 turns with compaction to finish (`docs/phase1-results.md:64-77`). Two things P2 adds to #637's spec:
report how often `elide` alone keeps the context under `summarize_at` (that is the number P2 Figure 5 uses), and
try widening the elide-to-summarise gap in config (`elide_at`, `summarize_at` are live keys) before adding rules,
because P2's efficiency came from the gap as much as from the rules.

### 4.3 "Recoverable content adds nothing": what it means for masking and `read_artifact`

P2 Finding 3 (P2 §3.2, Table 13): T2 minus T1 averaged -0.36 points over 32 comparisons (+0.40 on SWE-Bench, -1.12
on Terminal-Bench; 15 wins, 14 losses, 3 ties); 36 of 64 T2/T4 settings never called `recall_event`; mean calls per
task fell from 0.540 at 32k to 0.069 at 64k, 0.011 at 96k and 0.007 at 128k; the calls that did happen were almost
all from the weakest model under the tightest window. P2 concludes that lossless storage "adds machinery most
models seldom use".

What transfers to the harness, and what does not:

- **The window regime is the same.** The native local model runs at 65,536 tokens (`config/harness.yaml:58-63`),
  P2's 64k cell, where recall averaged 0.07 calls per task. The harness has no measurement of its own:
  `read_artifact` calls are not counted anywhere (no metric in `harness/metrics.py`, no field in `turn_metrics`).
  The only proxy is the replay's "needed again" column, 1 of 9 masked items (`docs/jev-compaction-study.md:67`),
  on transcripts that predate masking.
- **The harness masks far more than P2 elided.** P2's T2 stubbed only middle-region observations once the window
  bound. The harness replaces every used successful result over 2000 chars on every turn, so the share of the
  model's past that sits behind a receipt is much larger, and the recovery path matters more in principle than in
  P2. P2's result therefore does not license removing recoverability; it says the model will rarely use it, which
  is a different claim.
- **The cost side is small.** P2's "machinery" was a file store and a tool; the harness's is one `artifacts` table
  (`harness/db.py:58`), a 200-char receipt and one schema. The receipt itself is the only per-turn cost, and it is what
  makes masking deterministic and cache-friendly (the replay estimated 89.7% prefix reuse for receipts against
  84.3% for a relevance selector, `docs/jev-compaction-study.md:64-68`).
- **The summariser prompts differ in a way that matters.** P2's summariser is told "the raw events remain
  retrievable, so summarize, don't transcribe" (P2 Figure 18), leaning on recall. The harness's `SUMMARY_PROMPT`
  (`harness/compaction.py:21`) does the opposite: keep every intermediate value the task needs. The harness's design is the
  safer one if recall is rarely used, and P2's numbers support keeping it.

Decision for this report: keep masking and `read_artifact` as they are, add the missing count (F2), and revisit
once the count exists. If `read_artifact` stays near zero over a run of ordinary sessions, the receipt can shrink
(drop the argument preview and digest) and the schema can move behind deferred loading (F9); if it is used, P2's
finding did not transfer and the mask minimum can come down. For #637, P2's result argues against any new rule
that stores what it removes (the draft "superseded file reads" rule should drop the older read, not archive it);
masking already covers recovery for anything large.

### 4.4 Is a bash-only profile plausible for hosted backends?

P2 Finding 5 (P2 §3.2, Figure 7, Tables 3 and 4): bash-only helped only the strongest model and one benchmark
pairing, and hurt everything else.

| Model | SWE-Bench, T4 128k, full tools vs bash-only | Terminal-Bench, same |
| --- | --- | --- |
| Nemotron-3 30B | 25.2% / $0.09 vs 10.2% / $0.03 | 13.5% vs 3.4% |
| Nemotron-3 120B | 44.0% / $0.34 vs 42.4% / $0.35 | 28.1% vs 23.6% |
| Nemotron-3 550B | 65.8% / $2.33 vs 69.4% / $1.11 | 44.9% / $2.43 vs 50.6% / $1.70 |
| Mistral-Medium-3.5-128B | 68.6% / $3.14 vs 45.4% / $1.72 | 37.1% / $2.22 vs 43.8% / $2.75 |

(P2 Tables 3 and 4.) The 550B saving came from fewer, denser actions: 32% fewer calls, re-patches per task from 4.6
to 1.5 (P2 §4, Tables 11 and 12). Mistral's SWE-Bench drop came from file-localisation failures rising from 16.0% to
41.4% (P2 Table 10). P2 §6 warns the crossover points "should be validated before being transferred".

Plausibility per backend, from the argv the harness builds today:

- **Claude Code, builtin mode**: plausible. `ClaudeSession.command` already passes `--tools ""` for tools-only
  sessions and `--tools "Task,TodoWrite"` in split mode (`harness/cli_backends.py:182-184`); `--tools Bash` is the same
  mechanism. Claude Code's `Bash` is the one tool the smart-approvals reviewer is tagged for (`harness/policy.py:45`), so
  the approval path is unchanged.
- **Claude Code and Codex, split mode**: plausible and cleaner. In split mode the CLI has no workspace tools of its own
  (`CodexSession` starts with `environments: []`, `harness/cli_backends.py:416, 526`) and the harness MCP server serves
  `SPLIT_TOOLS` to both backends (`Runner.split_schemas`, `harness/runner.py:473`; `harness/mcp_server.py:44`). A bash-only profile is
  `SPLIT_TOOLS` reduced to `run_shell`, and the sandbox, policy and grants stay as they are. This is the version to
  build first, since it needs no vendor flag and covers Codex as well.
- **Codex, builtin mode**: not plausible from current wiring. The harness controls Codex's native tools through
  `environments: []` and feature overrides (`harness/cli_backends.py:323-337, 516-560`), which remove the shell together with
  `apply_patch`, so there is no "shell but no editor" configuration without a vendor tool allowlist the harness does
  not use today. List builtin-mode Codex as unsupported in #638 unless the owner finds such a flag in the pinned Codex.
- **Cursor**: no tool flag is passed (`harness/cli_backends.py:763`) and it is outside MCP (`MCP_BACKENDS`,
  `harness/mcp_server.py:41`). Unsupported.
- **OpenCode** (`kind: cli`): its managed permission config already denies `webfetch`, `websearch` and `question`
  (`OpencodeJsonl.config`, `harness/cli_backends.py:870`). Denying its edit and read permissions the same way is plausible
  but must be checked against the pinned OpenCode 1.18 permission keys before #638 promises it. It runs the local
  model, which is the "weaker, bash-weak" case P2 says loses 15 points, so it should be a test target only.
- **Native local sessions**: do not apply. The native loop runs a 35B local model, P2's weak-model regime, where
  the predefined set won by 15.0 points on SWE-Bench and 10.1 on Terminal-Bench. The harness's default profile also
  lacks P2's read-before-write and post-edit diagnostics, so it sits between P2's two conditions.

Verdict: plausible for Claude Code in both modes and for Codex in split mode, opt-in, per session, with the comparison #638 already requires. The
comparison must break results down by task type (shell-centric against edit-centric) because P2's gains were
benchmark-dependent, and it should log localisation failures, the failure mode P2 saw grow.

### 4.5 The planning finding and the harness

P2 Finding 4 (P2 §3.2, Figure 6, Table 5): planning added 11.6 points for the weakest model on SWE-Bench and 4.5 on
Terminal-Bench; without it 68.6% of its runs ended without an edit (P2 §4). For the two strongest models planning
cut cost by about 30% and 32% with success changes of -2.0 and -0.4 points, mostly by removing post-edit
verification turns (P2 §4, Table 5). The harness's `update_state` carries a `plan` and `next_step` (`harness/state.py:14-38`)
but re-injects them only at a round reset (`apply_round_reset`, `harness/compaction.py:207`); P2 re-injected the plan every
turn without storing it in history. The native local model is in the regime where P2's planning gain was largest.
That is follow-up F4, scoped as an experiment in fake-model mode and the canary, because the gain is model-specific
and P2 §6 says so.

### 4.6 Context management matters more as the window shrinks

P2 Finding 1 (P2 §3.2, Tables 3 and 4, Figure 3): the managed-minus-unmanaged gap on SWE-Bench was 35.7, 15.9, 5.5
and 2.7 points at 32k, 64k, 96k and 128k, and unmanaged overflow fell from 78.7% to 8.7%. At the harness's 64k
local window compaction is load-bearing (about 16 points by P2's numbers), which is consistent with Phase 0, where
OpenCode's compaction at 32k lost `log_correlation` for both models (`docs/phase0-results.md:127-130`). Hosted
sessions never run `_maybe_compact` (`harness/runner.py:984-985, 1236`); they rely on the vendor CLI's compaction at a
200k window, where P2 says the choice of strategy matters little. No change is proposed there.

## 5. Ranked follow-ups

Impact is for the native local-model path unless stated, since that is where both papers' findings bind. Effort
is in worker-sessions; risk is the chance of making sessions worse.

| Rank | Follow-up | Issue | Impact | Effort | Risk |
| --- | --- | --- | --- | --- | --- |
| F1 | Rule-based trim before summarisation (existing) | #637 | High | Medium | Low |
| F2 | Count what compaction and editing actually cost: `read_artifact` calls, `edit_file` mismatches, elide-only turns | #639 | High (decides F1 rules, Rec. 5, Section 4.3) | Low | Low |
| F3 | Pin the prior summary in the summary request; compact reactively when the server rejects the prompt | #640 | Medium | Low | Low |
| F4 | Per-turn plan re-injection for local sessions, as an experiment | #641 | High if P2's gain transfers | Medium | Medium |
| F5 | `AGENTS.md` and `CLAUDE.md` discovery for native sessions | #642 | Medium | Low | Low |
| F6 | Cheap stuck caps: identical failing calls and consecutive invalid tool calls | #643 | Medium | Low | Low |
| F7 | Bash-only tool profile for hosted backends (existing) | #638 | Medium (hosted cost) | Medium | Medium |
| F8 | Delimit MCP and session-search results as untrusted content | #644 | Medium (security) | Low | Low |
| F9 | Measure schema overhead and defer rarely used tool schemas | #645 | Medium | High | Medium |

Not filed, with reasons: a middleware pipeline for the loop (Rec. 1) and an ACP server (Rec. 13) both touch the
runner/server boundary and are **after #334**; a fuzzy edit cascade (Rec. 5) waits for F2's mismatch count; code
RAG and sub-agents are absences the papers endorse.

**F1, #637 (rule-based trimming before summarisation).** The order already matches P2's T4, so #637 is about the
rule tier's reach, not its position. Changes the report makes to its body: (a) measure and report elide-only
turns, the share of compaction passes where `elide` kept the context under `summarize_at` without a summary call,
with trim on and off, since that is P2's efficiency mechanism (Figure 5); (b) include a config-only arm that widens
the gap between `elide_at` and `summarize_at` (both live keys) in the benchmark, so a rule is only added if it
beats the free change; (c) the "superseded file reads" rule drops the older read rather than archiving it, per
Section 4.3; (d) the head-and-tail rule should report how often a tool is re-run with the same arguments after
trimming, the "needed again" proxy from `docs/jev-compaction-study.md`. Everything else in #637 stands.

**F2, #639 (compaction and edit telemetry).** Add three counters to the per-turn metrics payload and session
totals: `read_artifact` calls (and whether the slice was later followed by a second slice of the same digest),
`edit_file` failures with the `exactly once` error, and compaction passes that ended at the elide tier. Export them
as Prometheus counters next to `harness_compaction_total` and show the first two in the session's efficiency
summary. No behaviour change. This is the measurement Section 4.3 and Rec. 5 both need; without it the harness
cannot say whether its recoverability or its exact-match contract is paying off, and P2 shows the answer is
model-specific. Mechanical, fully specified, `ready`.

**F3, #640 (summary continuity and reactive compaction).** Two small changes in `harness/compaction.py` and the
runner. First, `summary_request` passes the previous `[Context summary]` message as a separate, untruncated
"prior notes" block and tells the summariser to update it rather than restate it (P2's Figure 18 wording; P1 Rec.
7's incremental merge), so that `render_excerpt` can no longer drop it first. Second, when `llm.chat` raises a
non-retryable error whose body says the prompt exceeds the context (llama-server's message), the runner runs one
forced compaction pass at the summary tier and retries once before failing the run, which is Rec. 7's "fire
reactively on overflow" and today's gap. Both are testable on synthetic message lists and in fake-model mode.

**F4, #641 (per-turn plan re-injection, experiment).** For native local sessions only and behind a config flag
off by default, prepend the saved state's `plan` and `next_step` (from `update_state`) as a short ephemeral user
message immediately before each model call, without storing it in the session context, matching P2 §2.1. Measure
in fake-model mode (the fake model receives the injection) and in the canary (pass rate, turns, prompt tokens
against the nightly history), since P2 Finding 4 says the gain is large for weak models and may cost tokens. Needs
refinement: the owner should decide whether the system prompt should ask the model to keep its plan current, and
whether the injection goes before or after the newest tool results (cache implications are small either way since
it is at the tail).

**F5, #642 (context-file discovery for native sessions).** At session start, read `AGENTS.md`, then `CLAUDE.md`,
at the workspace root, capped at the same 16,000 characters `cli_backends.project_memory` uses, and append the
text to the system prompt after the project `instructions`, labelled as repository guidance. Refresh at round reset
only, like the repo map, so the prefix stays cache-stable within a round. Nested files and just-in-time injection
are out of scope. This is the one Listing 3 feature the harness lacks (Section 3) and P1 Rec. 6; it is also what
makes the harness's own `AGENTS.md` visible to its own native sessions.

**F6, #643 (cheap stuck caps).** `RetryTracker` already recognises a call that repeats an earlier failure with the
same name and arguments. Add two caps in the runner: after three such repeats in a run, feed back a one-line nudge
with the tool result; after eight, end the run with a `stuck` reason, mirroring P2's stuck-detection reminders
(P2 Figure 20) and P1 Rec. 18's "cheap caps" (OpenCode's three-identical-calls ask). Also cap consecutive invalid
tool calls (bad JSON or unknown tool) at five with the same ending. Thresholds in config, events for both, no
LLM judge.

**F7, #638 (bash-only profile for hosted backends).** Changes the report makes to its body: (a) split mode is the
first target for both Claude Code and Codex, implemented as `SPLIT_TOOLS` reduced to `run_shell`, since it needs no
vendor flag; (b) Claude Code builtin mode passes `--tools Bash`; (c) Codex builtin mode is unsupported from current
wiring (`environments: []` removes the shell with the editor) unless the owner finds a tool allowlist in the pinned
CLI; Cursor is unsupported; OpenCode is a test target only, pending a check of its permission keys; (d) the comparison script groups by task type and
records localisation failures, because P2's gains were benchmark-dependent and that failure mode grew; (e) the
docs must say the profile is for strong hosted models only and must never be applied to native local sessions.

**F8, #644 (untrusted-content delimiting beyond web results).** Prefix results of `mcp__*` tools and `session_read`
with the same `UNTRUSTED` banner `harness/web_tools.py:51` uses, since both already taint the session (`harness/runner.py:2708-2720`)
and P1 pattern 28 (Hermes, OpenHands) wraps all such results. Keep the banner outside the stored artifact so
masking digests are unchanged. One constant, two call sites, tests on the rendered tool message.

**F9, #645 (schema overhead and deferred loading).** The runner already computes the tool-schema token overhead
per turn (`_maybe_compact`, `harness/runner.py:3187-3188`). First record it per session and per toolkit set; then, if the
overhead with common toolkits exceeds a few percent of the window, defer the rarely used schemas (`update_notes`,
`verify` when no checks are configured, homelab, memory writes, MCP catalogues) behind a `tool_search` tool, as
P1 Rec. 4 and Observation 8 describe for Claude Code, Codex and Hermes. Needs refinement: the deferral mechanism
changes the prompt for every native session and must be measured in the canary first.
