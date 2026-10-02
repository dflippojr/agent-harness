# Observability: session traces (#259)

Prometheus metrics (`GET /metrics`, `harness/metrics.py`) show aggregate rates. A trace shows where one session's
wall time went: waiting for the GPU slot, reading the prompt, decoding, waiting for an approval, running a tool,
compacting, or sitting idle between runs. Traces are OpenTelemetry spans sent over OTLP/HTTP to Grafana Tempo in
the observability stack. They're off by default.

## Turning it on

```yaml
# config/harness.yaml
telemetry:
  otlp_endpoint: ""                # off; e.g. http://127.0.0.1:4318/v1/traces
  service_name: agent-harness      # resource service.name
  trace_url_template: ""           # optional Grafana Explore URL containing {trace_id}
```

- `otlp_endpoint` is the full OTLP/HTTP traces URL. Use loopback or the tailnet only; nothing is meant to leave
  the tower.
- The OpenTelemetry packages are optional: `pip install -r requirements-telemetry.txt`. They aren't in
  `requirements.txt`, so neither the base nor the service profile depends on them.
- If the endpoint is set but the packages are missing, the daemon logs one warning and runs without tracing.
- The settings are read at daemon start. Restart the daemon to apply a change.

Tempo, the Grafana datasource, and a traces dashboard are in [`ops/observability/`](../ops/observability/README.md).
Applying them to the live stack is an owner step.

### When it's off

`harness/telemetry.py` is the only module that mentions `opentelemetry`, and it imports it lazily, only when
`otlp_endpoint` is set. Otherwise every call site gets `NoopTracer`, whose spans do nothing. `import harness.runner`
loads no `opentelemetry` module. One caveat: FastAPI 0.142+ depends on `opentelemetry-api` and imports it itself,
so a running daemon has that API package loaded either way. The harness never loads the SDK or the exporter
while tracing is off (`tests/test_tracing.py`).

## Trace shape

One trace per session. When the session is created, its trace and root-span ids go in `run["trace"]`, which carries
over from run to run like the notes do. A follow-up message or a resume after a restart adds to the same trace.

```
session                      created → end of the latest run
├── idle                     end of one run → start of the next (waiting on the user)
├── run_setup                target wait, repo prepare, git baseline, repo map
├── gpu_slot_wait            queued for the single GPU slot (scheduler.acquire)
├── turn                     one model turn: compaction, the model call, and the tool calls it asked for
│   ├── compaction           elide / summarize / round reset (a summary adds its own `chat` child)
│   ├── gpu_slot_wait        re-queued after the GPU guard paused the model
│   ├── chat                 one model request
│   └── execute_tool         one tool call
│       ├── approval_wait    waiting for the user to approve or deny
│       ├── gpu_slot_wait    re-queued after the approval
│       └── sandbox_exec     the tool itself (sandbox, daemon toolkit, app, runner)
│           or image_job     an image tool on the image service
├── hosted_cli_turn          a Claude / Codex / Cursor CLI session, from start to its result
└── run_end                  transcript, branch save, sandbox stop
```

Sibling spans under a `turn` don't overlap. Tools run one at a time today. A tool source that runs calls in
parallel (MCP tools, #260) should open each call with `telemetry.tool_span(name, call_id, parallel=True)`, which
marks the span `harness.parallel=true`. In tests, the direct children of `session` cover its duration, and the
children of each `turn` cover the turn, both within 5%.

The root `session` span keeps the same ids for the life of the session and is exported again at the end of each
run with the later end time. Tempo keeps one copy of a span id. Until a session has finished its last run, the root
in Grafana can show an earlier end, but all the child spans are there.

Work outside a session isn't traced, for example image jobs started from the Images page or endpoint requests.

### Attributes

| Span | Attributes |
|---|---|
| `session` | `harness.session_id`, `harness.backend`, `gen_ai.request.model`, `harness.recovered`, `harness.status` |
| `turn` | `harness.turn` (1-based model turn in the run), `harness.resumed` (set on the turn that finishes tool calls left pending by a daemon restart) |
| `chat` | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `harness.cache_read_tokens`, `harness.prompt_ms` (prompt processing), `harness.decode_ms` (generation) |
| `execute_tool` | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `harness.policy_decision` (allow/ask/deny), `harness.ok` (false when blocked or denied), `harness.output_chars`, `harness.parallel` |
| `approval_wait` | `harness.approval_status` |
| `sandbox_exec`, `image_job` | `gen_ai.tool.name` |
| `gpu_slot_wait` | `harness.queue_front` |
| `compaction` | `harness.compaction_tier`, `harness.tokens_before`, `harness.tokens_after` |
| `hosted_cli_turn` | `harness.backend`, `gen_ai.request.model` |
| any failed span | `error.type` (the exception class) and status ERROR |

`harness.prompt_ms` and `harness.decode_ms` come from llama.cpp's `timings`. They're missing when the server
doesn't report them.

## Privacy allowlist

Spans carry ids, names, sizes, counts, and timings only. `telemetry.ALLOWED_ATTRIBUTES` is the allowlist, and
any other key is dropped, whatever the caller passes. Non-scalar values are dropped too, and strings are capped at
200 characters. These never go in a span:

- prompts, completions, reasoning, or the session title
- tool arguments, tool output, or file contents
- file paths
- error messages: a failure records only the exception class (`error.type`), with no status description and no
  exception event or stack

`tests/test_tracing.py` runs sessions whose prompt, title, tool arguments, file contents, tool errors, and final
answer contain a sentinel string, then checks that it shows up in no span name, attribute key, attribute value, or
status. Another test checks that keys outside the allowlist are dropped. To add an attribute, add its key to
`ALLOWED_ATTRIBUTES`, then add it to the table above and to the tests.

## Info tab

The session API (`GET /sessions/{id}`) returns `trace_id`, which is `""` when tracing is off. When
`trace_url_template` is set, the API also returns `trace_url`, the template with `{trace_id}` filled in. The
session's Info tab (`#/s/<id>/info`) shows a Trace row: a link to `trace_url`, or, without a template, the bare id
with a Copy button. With tracing off, there's no row.
