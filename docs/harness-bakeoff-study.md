# Harness bake-off study (draft, stage A of #227)

This is a compatibility and adapter record, not a scored study. No model was
loaded, no GPU was used, and no production or staging daemon was changed.
Smoke runs, scored runs, recommendations, and the hosted lane remain in #227.

## Frozen candidate revisions

Checked on 2026-10-04 against the upstream latest non-prerelease release. Runtime
containers cannot update themselves. Image builds may fetch public dependencies;
task execution has only the internal model network, with network-less checkers.

| Candidate | Release / revision | License | Compatibility |
| --- | --- | --- | --- |
| Minimal baseline | Agent Harness `10a44365778dd599eb0d5593939211523033b7ed` (this worktree's base) | MIT | Existing `bakeoff.agent`; separate baseline, not registered as the current harness |
| Current Agent Harness | Same base plus the commits in this PR; save `git rev-parse HEAD` with scored artifacts | MIT | `agent-harness`: wraps `CanaryRunner`, through Manager and production runner |
| OpenHands CLI | [1.16.0](https://github.com/OpenHands/OpenHands-CLI/releases/tag/1.16.0), `2963442dacc7cea44e39b7c4e73724295c853465` | MIT | `openhands`; Phase 0 was also 1.16.0; no newer upstream release exists at this check |
| OpenCode | [v1.18.34](https://github.com/anomalyco/opencode/releases/tag/v1.18.34), `aec0b9a6d8898f68f923aaf08b7306d931fd9d76` | MIT | `opencode`; Phase 0 was 1.18.30 |
| Unreal Agent | [v0.2.0](https://github.com/unreallabsai/unreal-agent/releases/tag/v0.2.0), `1b9f778453f411c029b39b85102aaefb95e7e48d` | MIT | Excluded: OpenAI/OpenRouter/Fireworks adapters require `/responses`; Ollama requires `/api/chat`. Neither speaks the study's shared `/v1/chat/completions` endpoint. No protocol translation or candidate fork added |
| OpenClaw | [v2026.9.8](https://github.com/openclaw/openclaw/releases/tag/v2026.9.8), `fc23bc864e4553c2d215e479eeec47b67a0bf943` | MIT | `openclaw`, native local agent, no gateway daemon |
| Hermes Agent | [v2026.9.24](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24), `f97608f178d1ffeca59860195ab7da295f7c8e5f` (package 0.21.5) | MIT | `hermes`, custom OpenAI-compatible endpoint. Requires at least 64K context in custom-provider mode; a 32K local study cell is incompatible and must be recorded as excluded, rather than claiming the server has a larger window |

The Unreal exclusion follows its pinned
[OpenAI client](https://github.com/unreallabsai/unreal-agent/blob/1b9f778453f411c029b39b85102aaefb95e7e48d/harness/llm/clients/openai/client.go)
and [provider registry](https://github.com/unreallabsai/unreal-agent/blob/1b9f778453f411c029b39b85102aaefb95e7e48d/cmd/internal/agentrunner/providers.go).
Hermes' minimum is enforced in
[agent_init.py](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/agent/agent_init.py).
These are compatibility exclusions, not requests to alter native agents or launch settings.

## Headless interfaces and native settings

| Adapter | Prompt and workspace | Endpoint configuration | Native system prompt characters observed on `repo_qa` | Tools / sampling / context / compaction |
| --- | --- | --- | --- | --- |
| Minimal baseline | `Agent.run(task.prompt)`, fresh fixture in sandbox | `base_url` | Not measured in stage A; existing result has `system_prompt_chars` | Baseline tools and model-profile sampling; 32K server profile; separate existing baseline runner |
| agent-harness | CanaryRunner creates session, common prepared fixture copied into its workspace before the first await; result copied back | Throwaway Config / ModelConfig; localhost model port, no production config loaded | 1,109 | 15 tool schemas on this task; ModelConfig sampling defaults (no temperature field sent); context 32,768 / output 8,192; native masking at 55%, summary at 65%, round reset at 60%, minimum mask 2,000 chars |
| openhands | `openhands --headless --json --override-with-envs -t PROMPT`, `/workspace` | `LLM_MODEL=openai/MODEL`, `LLM_BASE_URL`, synthetic `LLM_API_KEY=local` | 35,610 | 7 schemas, terminal/file editor/skills/task tools; native sampling (no temperature field sent); native context/condensation policy; CLI does not expose a task turn cap in this adapter; task wall limit enforced |
| opencode | `opencode run --format json --auto -m llama/MODEL -- PROMPT`, `/workspace` | `opencode.json` `{env:LLM_BASE_URL}` | 9,530 | 9 schemas; native sampling (no temperature field sent); configured context 32,768 / output 8,192; native auto-compaction; web and question permissions denied as in Phase 0 |
| openclaw | `openclaw agent --local --agent main --session-id bakeoff --message PROMPT --json`, `/workspace` | `config.json` `{env:LLM_BASE_URL}` / `{env:LLM_MODEL}`, `api=openai-completions` | 38,641 | 12 visible schemas, native tool-search catalog and bootstrap; native sampling (no temperature field sent); context 32,768 / output 8,192; native compaction and retry/fallback behavior |
| hermes | `hermes chat --provider custom --model MODEL --format stream-json --query PROMPT --max-turns N`, terminal cwd `/workspace` | `config.json` env references resolved to throwaway config; `OPENAI_BASE_URL` / synthetic `OPENAI_API_KEY=local` | 12,202 | 17 schemas on this task, native toolsets; native sampling (no temperature field sent); context 65,536; native compression enabled, small-window threshold floor 75%; configured task turn cap |
| unreal-agent (excluded) | Runner supports `-p PROMPT -workspace PATH`, JSONL stdout | `UNREAL_HARNESS_LLM_BASE_URL`, but incompatible wire protocol | Not measured; no runnable adapter | Native Bash/ViewImage/SkillUse registry; remaining measurements unavailable because the endpoint compatibility gate failed |

Prompt sizes are characters in the actual first fake-model request, not token
counts from the fake's scripted usage. They include candidate-added bootstrap,
skills and task-independent runtime context and can vary with date and task.
Full prompts and tool schemas are captured in fake request artifacts for audit.
For scored runs, archive the image IDs, these configs, current harness SHA,
server profile, and raw events before the smoke/scored freeze. Do not upgrade
between smoke and scoring.
OpenHands' public-skills checkout is also pinned to
`3ed88619ff2b4bccdf95a88f1dbc6e7182bf365d`; Phase 0 cloned an unrecorded `main`.

OpenClaw uses a native Linux workspace inside its container because its safe
write helper's rename semantics fail on Windows Docker bind mounts. The wrapper
copies the identical prepared fixture in and the resulting tree back (including
deletions), then the common checker grades that returned tree. No daemon starts.
Hermes uses its supported editable source installation at the pinned revision.

## Fake endpoint and verification

The standard-library `bakeoff.fake_endpoint` serves `/v1/chat/completions` in
streaming and non-streaming modes, including scripted function calls, usage,
HTTP errors, delays and exhaustion. It has no model code. An optional
`auxiliary` script handles native title-generation requests without advancing
the task script. It logs every request with the selected step index; auxiliary
requests have a null index. It rejects other inference protocols.

`FakeEndpoint` owns uniquely named containers and an internal Docker network.
Like the existing socat pattern, only its endpoint container joins the default
bridge and publishes a localhost port for the in-process Manager. Candidate
containers join only the internal network; they have no internet or LAN route.
The hidden checker stays on `--network none`. Resources remain 4 GB / 2 CPUs
for candidate containers and 2 GB / 2 CPUs for tool/checker sandboxes.

Build the sandbox and candidate images before running the opt-in integration
tests. They never build images, download models or contact a real model server:

```powershell
python -m pytest tests/test_bakeoff_adapters.py -q
$env:BAKEOFF_DOCKER_TESTS = '1'
python -m pytest tests/test_bakeoff_adapters.py -q -m docker
```

Each integration case uses the existing core `repo_qa` task, calls a native tool
against the fresh fixture, gets a scripted final answer, and invokes the hidden
checker. Parser tests also cover model errors, wall limits and classification.
Raw local test artifacts live under gitignored `runs/`; no credentials or
private prompts are included in fixtures. The executable help lists five
registered adapters; Unreal's reason is recorded above.

For a manual no-model reference run:

```text
python -m bakeoff.reference --harness opencode --suite core --tasks repo_qa --fake-script SCRIPT.json --no-build
```

This mode skips LlamaServer, GpuSampler and MemorySampler (the latter also probes
VRAM). Supply scripted responses for every task/repeat; scripts fail closed
when exhausted. Runs without `--fake-script` remain GPU work under #227's gates.

## Reporting and telemetry limits

Saved records and reports preserve per-task/per-repeat checker verdicts and
failure classes: `model`, `harness/tooling`, `adapter`, `infrastructure`. Reports
show per-repeat completion/timeout/error counts, summed calls/turns/retries,
compaction/masking/failures, prompt/output/context tokens, median and nearest-rank
p90 wall time, and cell peak RAM/VRAM. Rescoring updates verdicts and failure
classification while retaining execution telemetry; it never calls a model.
RAM is the existing sampler's system peak commit, not container RSS. VRAM is
the existing sampler's system peak usage. Fake runs record these as unknown.

Manager exports its native session totals, context count, tool results, retry,
compaction and masking events. OpenCode exports step token usage and tool events.
OpenClaw exports native assistant-turn, usage, tool-summary, execution-trace and
system-prompt reports (new releases store sessions in SQLite rather than JSONL).
Hermes exports native stream tool/results plus native SQLite accounting for
turns, compactions and compression failure state; auxiliary tokens are included
in totals without counting title/compression calls as turns.

Absent telemetry stays `unknown`, never a fabricated zero. In particular,
OpenHands' CLI events do not expose aggregate token usage; OpenCode/OpenClaw
do not expose every masking/compaction-failure event; Hermes' CLI lacks final
context-size and retry counts. Do not use these missing fields to claim parity
or a performance result. Checker errors are infrastructure failures; missing
terminal envelopes are adapter failures; a failing checker after successful
execution is classified as a model failure. Timing caps count as timeouts.

The stage B section will add smoke exclusions, scored matrices, repeat variation,
Phase 0 comparisons, hosted costs (only after its owner gate), and recommendations.
