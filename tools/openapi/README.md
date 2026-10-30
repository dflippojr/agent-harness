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
