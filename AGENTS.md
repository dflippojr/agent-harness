# Local checks

With project Python dependencies and `tools/openapi/requirements.txt` installed, run `python tools/openapi/export_schema.py --check` to verify the committed OpenAPI document.

Stables, the browser client, lives in its own repository, dflippojr/stables (#334). After changing API routes or models, regenerate the document with `python tools/openapi/export_schema.py --write` and commit it; Stables pins a copy. This repository pins a Stables release in `tools/stables/pin.json` (see `tools/stables/README.md`): the tower deploys it, the Playwright journeys in `tools/ui-tests` run against it, and the management-parity tests read its API calls from `tools/stables/api-calls.json`. The Server's own tests serve a stand-in shell (`tests/fixtures/stables_shell`).

# How to prove a change works

CI (`.github/workflows/ci.yml`) is the gate. Its `gate` job is the one check to require: it always runs, needs every
other job, and fails if any of them failed or was cancelled. Run the same jobs locally before you push. Keep test
parallelism at 4 workers or fewer.

| CI job | Local command (repository root) |
| --- | --- |
| `test` | `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt pytest-cov pytest-xdist`, then `python scripts/docs/build.py --check --base origin/main` and `python -m pytest tests -q -n 4 --dist loadfile --cov=harness --cov=harness_modules --cov=sdk --cov=scripts --cov=macrunner --cov-report=term --cov-fail-under=88`. Coverage below 88% fails the job. For one file: `python -m pytest tests/test_x.py -q`. |
| `test-py313` | On Python 3.13, `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt pytest-xdist`, then `python -m pytest tests/test_import_smoke.py -q -p no:xdist --noconftest` and `python -m pytest tests -q -n 4 --dist loadfile`. No coverage. |
| `lint` | `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt -r tools/lint/requirements.txt`, then `python -m ruff check .` and `python tools/lint/pyright_baseline.py`. Fix a ruff finding, or silence that one line with `# noqa: <rule>` and a reason; no per-file ignores. Pyright fails only on errors its baseline (`tools/lint/pyright-baseline.json`) does not record: fix new ones rather than growing it. After fixing recorded ones, shrink it with `python tools/lint/pyright_baseline.py --write`. |
| `openapi` | `python tools/openapi/export_schema.py --check` (see Local checks above). |
| `ui-tests` | Check out the Stables tag pinned in `tools/stables/pin.json` and set `HARNESS_STABLES_DIR` to its `public/`. Then `python tools/stables/pin.py --check <stables checkout>`, `npm --prefix tools/ui-tests ci`, `npm --prefix tools/ui-tests exec -- playwright install chromium webkit` and `npm --prefix tools/ui-tests test -- --workers=1`. |
| `sonar` | CI only (SonarCloud). On a PR, list its new issues at `https://sonarcloud.io/api/issues/search?componentKeys=dflippojr_agent-harness&pullRequest=<PR>&resolved=false`. |
| `changes`, `gate` | CI only (`scripts/ci_gate.py`). A change touching only the files in `CI_SKIP_UNREAD_PATHS` skips the three test jobs, and `gate` still passes. |

To see a change working in the running app, start a disposable fake-model instance (no real model, no GPU, loopback
only), as `docs/ui-testing.md` describes:

```
python -m harness.fake_instance --dir "$TEMP/harness-proof"     # Git Bash; PowerShell: --dir "$env:TEMP\harness-proof"
python -m harness.fake_instance stop --dir "$TEMP/harness-proof"
```

It prints the Server and Web URLs. Stop it before you report.

Every PR body fills the `## Proof` table from the shared PR template (dflippojr/.github#13):

- **Acceptance criterion:** one row per criterion in the issue.
- **How verified:** the command, test name, or verify skill.
- **Evidence:** an output excerpt, artifact or run link, or red-before/green-after.
- **`### Not verified`:** what you did not check, and why (for example the Docker sandbox, the GPU, or hosted CLI
  backends, which CI never runs).
- **`### Gate changes`:** tests deleted or skipped, or config loosened, with the reason. Leave it empty if there are none.
