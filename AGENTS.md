# Local checks

With project Python dependencies and `tools/openapi/requirements.txt` installed and `npm --prefix stables/types ci` completed, run `npm --prefix stables/types run check` to verify generated API contracts and type-check Stables. Set `PYTHON` to your project interpreter if it is not on PATH.

Stables ships native JavaScript without a build step. Its type tooling and generated declarations live in `stables/types`, outside the served `stables/public`; the Server's OpenAPI exporter stays in `tools/openapi`. After changing API models, regenerate with `npm --prefix stables/types run generate` and commit both generated files.
