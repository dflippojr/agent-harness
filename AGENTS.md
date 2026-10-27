# Local checks

With project Python dependencies and `tools/openapi/requirements.txt` installed, run `python tools/openapi/export_schema.py --check` to verify the committed OpenAPI document.

Stables, the browser client, lives in its own repository, dflippojr/stables (#334). After changing API routes or models, regenerate the document with `python tools/openapi/export_schema.py --write` and commit it; Stables pins a copy. This repository pins a Stables release in `tools/stables/pin.json` (see `tools/stables/README.md`): the tower deploys it, the Playwright journeys in `tools/ui-tests` run against it, and the management-parity tests read its API calls from `tools/stables/api-calls.json`. The Server's own tests serve a stand-in shell (`tests/fixtures/stables_shell`).

# How to prove a change works

CI (`.github/workflows/ci.yml`) is the gate. Its `gate` job always runs, needs every other job, and fails if any of them
failed or was cancelled. The gate-guard check is `gate-guard-target / gate-guard`, the job in
`.github/workflows/gate-guard-target.yml`, which runs from `main` under `pull_request_target`, so a pull request can't
replace its own judge. Require both checks, `gate` and `gate-guard-target / gate-guard`. Run the same jobs locally before you push. Keep test parallelism at 4 workers or fewer.

| CI job | Local command (repository root) |
| --- | --- |
| `test` | `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt pytest-cov pytest-xdist`, then `python scripts/docs/build.py --check --base origin/main` and `python -m pytest tests -q -n 4 --dist loadfile --cov=harness --cov=harness_modules --cov=sdk --cov=scripts --cov=macrunner --cov-report=term --cov-fail-under=88`. Coverage below 88% fails the job. For one file: `python -m pytest tests/test_x.py -q`. |
| `test-py313` | On Python 3.13, `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt pytest-xdist`, then `python -m pytest tests/test_import_smoke.py -q -p no:xdist --noconftest` and `python -m pytest tests -q -n 4 --dist loadfile`. No coverage. |
| `lint` | `python -m pip install -r requirements.txt -r requirements-repomap.txt -r requirements-telemetry.txt -r tools/lint/requirements.txt`, then `python -m ruff check .` and `python tools/lint/pyright_baseline.py`. Fix a ruff finding, or silence that one line with `# noqa: <rule>` and a reason; no per-file ignores. Pyright fails only on errors its baseline (`tools/lint/pyright-baseline.json`) does not record: fix new ones rather than growing it. After fixing recorded ones, shrink it with `python tools/lint/pyright_baseline.py --write`. |
| `openapi` | `python tools/openapi/export_schema.py --check` (see Local checks above). |
| `ui-tests` | Check out the Stables tag pinned in `tools/stables/pin.json` and set `HARNESS_STABLES_DIR` to its `public/`. Then `python tools/stables/pin.py --check <stables checkout>`, `npm --prefix tools/ui-tests ci`, `npm --prefix tools/ui-tests exec -- playwright install chromium webkit` and `npm --prefix tools/ui-tests test -- --workers=1`. |
| `hub-contract` | Clone dflippojr/agent-harness-hub at the `ref:` pinned in `ci.yml`'s `hub-contract` job. In a venv of its own, from the Hub checkout: `python -m pip install --require-hashes -r requirements-e2e.lock -r requirements-build.lock` and `python -m pip install --no-deps --no-build-isolation -e .`. Then, still in the Hub checkout, `HUB_E2E_DAEMON_SRC=<this checkout> HUB_E2E_DAEMON_PYTHON=<a Python with this repository's requirements.txt> python -m pytest tests/contract -m e2e -v -p no:cacheprovider`. The suite starts this daemon itself, in fake-model mode on loopback with throwaway data. |
| `deploy-linux` | On Linux with `pwsh` 7 on PATH: `python -m pytest tests/test_deploy_ci_linux.py -q -p no:xdist --noconftest`. The module skips elsewhere. |
| `sonar` | CI only (SonarCloud). On a PR, list its new issues at `https://sonarcloud.io/api/issues/search?componentKeys=dflippojr_agent-harness&pullRequest=<PR>&resolved=false`. |
| `changes`, `gate` | CI only (`scripts/ci_gate.py`). A change touching only the files in `CI_SKIP_UNREAD_PATHS` skips the test jobs, and `gate` still passes. |

## Changing the API the Hub uses

The Hub (dflippojr/agent-harness-hub) is this daemon's main admin client. Its contract suite (`tests/contract` there;
`docs/daemon-contract.md` explains it) lists every endpoint the Hub calls, with the request it sends and the fields
and status codes it reads, and `hub-contract` runs that suite's `[daemon]` cases against each change here. A red
`hub-contract` means the change breaks the Hub: renamed or dropped fields, changed status codes or error codes, or a
removed endpoint. If the break is a mistake, fix the daemon. If it is deliberate:

1. Open a Hub PR first that updates the contract (`tests/contract/endpoints.py`), the Hub's fakes and the Hub for
   the new answer. The Hub's own CI runs its contract against the daemon commit it pins, so during the change the
   contract accepts both answers (the new field optional beside the old one, for example). Link the two PRs.
2. Once the Hub PR merges, bump the `ref:` of the Hub checkout in `ci.yml`'s `hub-contract` job, in this PR, to that
   Hub commit's full SHA. `hub-contract` turns green. The bump edits a workflow file, so the PR needs the owner's
   `gate-change-approved` label; say so under `### Gate changes`.
3. After this PR merges and the Hub bumps its daemon pin past it, a Hub follow-up drops the old answer.

Bump the Hub ref on its own (no daemon change) the same way when the Hub's contract grows, so new Hub expectations
judge this repository too.

To prove a change works in the running app, run the verify skill and paste its summary into the PR's Proof table:
`python .claude/skills/verify/scripts/verify.py run [--feature task]`. It starts a disposable fake-model instance on
loopback with synthetic data, checks it, drives the smoke journey on desktop and Pixel 7, writes
`.verify/<run>/summary.md` with screenshots and logs, and stops everything it started. Setup and the feature map are
in `.claude/skills/verify/SKILL.md`.

To poke at the app by hand instead, start the same disposable fake-model instance (no real model, no GPU, loopback
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
