# Web type checking

Agent Harness Web still ships the JavaScript files in `harness/web` directly. Nothing here is a build step, and this directory is outside both static mounts.

Install the project Python requirements plus `tools/web-types/requirements.txt`, then run:

```sh
npm --prefix tools/web-types ci
npm --prefix tools/web-types run check
```

Set `PYTHON` to your project Python interpreter when needed (for example `.venv/Scripts/python.exe` on Windows). The schema exporter creates a temporary full-profile configuration and database, never reads local daemon settings, and never starts the application lifespan or services.

After changing routes or response models, run `npm --prefix tools/web-types run generate` and commit `openapi.json` and `api.d.ts`. The generator pins FastAPI/Pydantic and Node tooling so CI can compare the checked-in artifacts with fresh output. Generation preserves the schema's optional fields even when they have defaults: responses using `response_model_exclude_unset` omit absent fields.

`client.mjs`, `lib/session.mjs`, `lib/stream.mjs`, and `pages/session.mjs` opt in. Other web modules currently carry `@ts-nocheck`. Strict null checking is enabled; implicit parameter types and caught errors remain permissive during this first migration. API calls for session list/detail, jobs, and admin discovery get generated response types through the request wrapper. Other endpoints, injected UI helpers, and SSE event payloads remain explicit incremental boundaries. The compile-only `type-tests.mjs` verifies that misspelled fields are rejected.
