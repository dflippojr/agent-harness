# Local checks

With project Python dependencies installed and `npm --prefix tools/web-types ci` completed, run `npm --prefix tools/web-types run check` to verify generated API contracts and type-check Agent Harness Web. Set `PYTHON` to your project interpreter if it is not on PATH.

Web ships native JavaScript without a build step. Type tooling and generated declarations live in `tools/web-types`, outside the served directory. After changing API models, regenerate with `npm --prefix tools/web-types run generate` and commit both generated files.
