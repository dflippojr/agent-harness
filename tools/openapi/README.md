# Server OpenAPI export

`export_schema.py` prints Agent Harness Server's full-profile OpenAPI document. It builds a throwaway configuration and
database, never reads local daemon settings, and never starts the application lifespan or services.
`requirements.txt` pins FastAPI and Pydantic so the output is byte-for-byte reproducible on developer machines and CI.

Clients consume the document; the Server only exports it. Stables' type tooling (`stables/types`) runs this exporter
and checks its committed `openapi.json` and `api.d.ts` against fresh output (`npm --prefix stables/types run check`).
