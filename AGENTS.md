# Local checks

With project Python dependencies and `tools/openapi/requirements.txt` installed, run `python tools/openapi/export_schema.py --check` to verify the committed OpenAPI document.

Stables, the browser client, lives in its own repository, dflippojr/stables (#334). After changing API routes or models, regenerate the document with `python tools/openapi/export_schema.py --write` and commit it; Stables pins a copy. This repository pins a Stables release in `tools/stables/pin.json` (see `tools/stables/README.md`): the tower deploys it, the Playwright journeys in `tools/ui-tests` run against it, and the management-parity tests read its API calls from `tools/stables/api-calls.json`. The Server's own tests serve a stand-in shell (`tests/fixtures/stables_shell`).

# Proving a change in the running app

To prove a change works in the real app, run the verify skill and paste its summary into the PR's Proof table:
`python .claude/skills/verify/scripts/verify.py run [--feature task]`. It starts a disposable fake-model instance on
loopback with synthetic data, checks it, drives the smoke journey on desktop and Pixel 7, writes
`.verify/<run>/summary.md` with screenshots and logs, and stops everything it started. Setup and the feature map are
in `.claude/skills/verify/SKILL.md`.
