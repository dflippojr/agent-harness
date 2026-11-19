# Server OpenAPI export

`export_schema.py` exports Agent Harness Server's full-profile OpenAPI document. It builds a throwaway configuration
and database, never reads local daemon settings, and never starts the application lifespan or services.
`requirements.txt` pins FastAPI and Pydantic so the output is byte-for-byte reproducible on developer machines and CI.
Dependabot bumps the FastAPI pin and the floor in the root `requirements.txt` in one PR, and
`tests/test_dependabot_config.py` fails if they differ. When a bump changes the document, `--write` and commit it on that PR.

```sh
python tools/openapi/export_schema.py --write   # rewrite openapi.json after changing routes or models
python tools/openapi/export_schema.py --check   # CI's `openapi` job: fail when openapi.json is stale
```

`openapi.json` is the committed document. Clients consume it; the Server only exports it. Stables
(dflippojr/stables) pins a copy in its `types/openapi.json` and type-checks against it
(`node types/generate.mjs --from <this file>` there).

## SSE event payloads

The event streams (`/sessions/{ref}/events`, `/api/v1/sessions/{ref}/events`, `/events`, `/api/v1/events` and their
admin aliases) are documented as `text/event-stream` responses whose frames carry `SessionEvent` (the session-list
streams: `SessionListEvent`), a discriminated union on `type` in the components (#1014). The schemas live in
`harness/event_schema.py`; they are documentation only and change nothing the Server emits.

`tests/test_event_schema.py` fails when the Server's code emits a type the union lacks, or the union keeps a type
nothing emits. During the test run, `tests/sse_event_capture.py` checks every event the Server emits against its
schema, so a payload field that contradicts the schema fails the test that emitted it. A new event type also needs a
recorded sample in `tests/fixtures/sse_event_samples.json`:

```sh
HARNESS_RECORD_SSE_EVENTS=.tmp/sse-events python -m pytest tests -q -n 4 --dist loadfile
python tools/openapi/record_event_samples.py .tmp/sse-events            # rewrite the samples
python tools/openapi/record_event_samples.py .tmp/sse-events --shapes   # or list each type's fields as emitted
```
