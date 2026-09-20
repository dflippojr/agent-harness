# GitHub CI and container images

`.github/workflows/ci.yml` is the single test workflow, and `.github/workflows/ci-cd.yml` publishes and deploys only
after that workflow succeeds for the exact commit pushed to `main`:

1. **CI / test** runs the complete test suite on the repository-scoped `agent-harness-ci` runner **pool** (three
   members on the tower) for pull requests and pushes to `main`. The job installs `pytest-xdist` as a CI extra (not
   in `requirements.txt`) and runs `python -m pytest tests -q -n 8 --dist loadfile`. There is no duplicate
   `windows-latest` test job in the image/deployment workflow.
2. **publish-images** is triggered by `workflow_run` only after `CI` completes successfully for a push to `main`.
   It checks out `github.event.workflow_run.head_sha`, never a branch name, and GitHub-hosted Linux builders publish
   both runtime images to GHCR:
   - `ghcr.io/dflippojr/agent-harness-sandbox:sha-<commit>` and `:py312`
   - `ghcr.io/dflippojr/agent-harness-cli:sha-<commit>` and `:1`
3. **deploy-tower** runs only after image publication for that same tested SHA. The repository-scoped runner pulls the
   immutable images, verifies that the live checkout is clean `main` and can fast-forward, builds a fresh side-by-side
   virtual environment, stops the daemon, swaps environments, fast-forwards, retags the images, starts the daemon, and
   waits for `/health`.

The SHA tags are immutable deployment inputs; the stable tags match the local names already expected by the daemon.
The CLI Dockerfile accepts `SANDBOX_IMAGE` so its hosted build is based on the exact sandbox image from the same commit.
The supervisor reads the ignored `.venv-path` file when present. Deployment switches that pointer rather than moving
the newly built environment, so Windows entry points keep their original absolute paths; the original `.venv` remains
untouched for first-deployment rollback. Later successful deployments remove the superseded managed environment.

Both builds read any available GitHub Actions cache, but cache export uses `ignore-error=true`. A `workflow_run` token
may be unable to write the default branch's Actions cache; that optional optimization must never block GHCR publication
or deployment. No later step consumes the exported cache. `publish-images` receives `contents: read` and
`packages: write`; `deploy-tower` receives only `contents: read`.
The GHCR packages are public (this repository is public), so the tower pulls the immutable `sha-<commit>` images
anonymously and does not sign in. The first production deployment showed why: `docker login ghcr.io` with the job's
`GITHUB_TOKEN` was rejected (`denied: denied`) on the tower, which failed the deployment before anything was changed.

## Deployment boundary

The daemon itself is deliberately not containerized. It is a host Python process because it coordinates Windows
scheduled tasks, local repository paths, Docker sandbox creation, GPU/model controls, and native provider logins.

The owner explicitly approved the repository-scoped self-hosted runners. The `agent-harness-tower` runner gives
trusted `main` workflow code the tower user's filesystem, Docker, credentials, and service-restart authority. It is
reserved for deployment; tests use the `agent-harness-ci` pool, SonarCloud uses GitHub-hosted Windows, and automated
review uses the `agent-harness-review` pool.

`.github/workflows/sonar.yml` is the analysis. It runs on GitHub-hosted `windows-latest` against SonarCloud
organization `dflippojr`, project key `dflippojr_agent-harness` (`sonar-project.properties`), host
`https://sonarcloud.io`, using the repository Actions secret `SONARCLOUD_TOKEN`. It installs the test extras, runs
`pytest` with `pytest-cov` Cobertura output (`coverage.xml`), then scans with `sonar.qualitygate.wait=true`. The
suite is Windows-native; Ubuntu hosted runners fail coverage collection on `msvcrt`, console-creation flags, and
Windows paths. Docker-image and GPU tests still skip on the hosted runner and do not contribute coverage. Non-Python
trees (`harness/web`, `ops`, scripts that are not `.py`) are excluded from the coverage metric. Confirm the SonarCloud
project still uses a gate that includes coverage on new code; the workflow cannot set that condition itself.

SonarCloud **Automatic Analysis must stay OFF**. This workflow is the analysis; turning Automatic Analysis on would
duplicate and fight it. To rotate the token, create a new SonarCloud user token, replace the repo Actions secret
`SONARCLOUD_TOKEN`, then confirm a `sonar` check on a pull request or a `main` push. The old `SONAR_TOKEN` and
`SONAR_HOST_URL` secrets are already deleted and must not be reintroduced.

Local SonarQube on `localhost:9000`, published by `ops/tailscale/serve.ps1` as `https://<tower>.ts.net:9000`, is
local-only. CI does not depend on it.

## Automated review backends

`.github/workflows/review.yml` reviews a pull request once when it is opened and supports explicit re-reviews through
`workflow_dispatch`. Re-review a PR with `gh workflow run review.yml -f pr_number=N` (optional `-f backend=…`).
`workflow_dispatch` always uses the workflow file on `main`, so unmerged `review.yml` changes are not exercised by
dispatch. Dispatch publishes a Check Run on the PR head SHA (#120/#124); `pull_request` opened already has the native
workflow check.

The optional `backend` dispatch input accepts `auto`, `cursor`, `codex`, or `claude`. An explicit
provider runs only that provider, which is useful for verification and deliberate quota steering. Omitting the input
or selecting `auto` tries the comma-separated `REVIEW_BACKENDS` repository variable in order. If the variable is empty,
the order defaults to `codex,claude,cursor`.

The runner falls through that ordered list when a CLI exits non-zero, returns no review, reports a recognizable
rate-limit or quota error, or omits the required completion marker after inspecting the diff. The marker is removed
before posting. The successful backend is included in the PR comment footer and the manually created Check Run. Invalid
backend names fail closed instead of silently changing provider.

Set `REVIEW_BACKENDS` under repository **Settings > Secrets and variables > Actions > Variables**. For example,
`claude,codex,cursor` spends Claude quota first while retaining two fallbacks; changing the variable does not require a
workflow edit.

All three CLIs run under the review runner service user and must be logged in for that same user. Cursor uses ask mode
with its sandbox enabled. Codex ignores the service user's configuration, restores only the required unelevated Windows
sandbox setting, disables apps and plugins, and supplies an empty MCP server table before entering its read-only sandbox.
Claude exposes only Read/Grep/Glob. The wrapper, rather than a model, writes the final comment file. It fetches the pull
request diff before starting a backend and embeds up to 200 KB of complete file patches directly in the prompt, so review
sandboxes do not need GitHub network access. Larger diffs identify every omitted file in the prompt. After installing or
changing a CLI, verify each backend explicitly against a disposable pull request:

```powershell
gh workflow run review.yml -f pr_number=N -f backend=cursor
gh workflow run review.yml -f pr_number=N -f backend=codex
gh workflow run review.yml -f pr_number=N -f backend=claude
```

Inspect each run and its PR comment. Record any CLI that is not runnable under the runner service user plainly in the
pull request verification notes; an interactive desktop login is not evidence that the runner service account is
authenticated.

`workflow_run` executes the workflow file from the default branch and can access secrets, so its jobs reject every
event except a successful `CI` run caused by a push whose head branch is `main`. They check out and deploy only the
reported `head_sha`; they never check out or execute pull-request code. Third-party actions are pinned to exact
commits, main deployments serialize rather than being canceled midway, and the GitHub `tower-production` environment
accepts deployments from `main` only.

## CI runner pool

Pytest (`.github/workflows/ci.yml`) uses three repository-scoped self-hosted runners that share the
`agent-harness-ci` label. `runs-on` is already `[self-hosted, Windows, X64, agent-harness-ci]`; expanding the pool
does not change the workflow selector. Each member takes one job, so tests for different refs can overlap instead of
serializing behind a single runner. `concurrency` remains `ci-${{ github.ref }}` with `cancel-in-progress: true`.

They are named, installed, and started as:

| GitHub name | Install dir | Hidden logon task |
| --- | --- | --- |
| `dflippotower-agent-harness-ci` | `D:\Agents\github-runner-ci` | `AgentHarness-GitHubRunner-CI` |
| `dflippotower-agent-harness-ci-2` | `D:\Agents\github-runner-ci-2` | `AgentHarness-GitHubRunner-CI-2` |
| `dflippotower-agent-harness-ci-3` | `D:\Agents\github-runner-ci-3` | `AgentHarness-GitHubRunner-CI-3` |

Default `-WorkDir _work` is correct: each member has its own checkout and `.venv` under that install dir. Do not
reuse an `InstallDir` that already contains `.runner`.

Use the existing parameterized installer (`ops/github/install-runner.ps1`). Obtain a fresh short-lived registration
token for **each** member (the token is single-use), then:

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token -Labels agent-harness-ci `
  -InstallDir D:\Agents\github-runner-ci -Name dflippotower-agent-harness-ci `
  -TaskName AgentHarness-GitHubRunner-CI
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token -Labels agent-harness-ci `
  -InstallDir D:\Agents\github-runner-ci-2 -Name dflippotower-agent-harness-ci-2 `
  -TaskName AgentHarness-GitHubRunner-CI-2
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token -Labels agent-harness-ci `
  -InstallDir D:\Agents\github-runner-ci-3 -Name dflippotower-agent-harness-ci-3 `
  -TaskName AgentHarness-GitHubRunner-CI-3
```

Each call consumes the token; request a new one for every member. The token is never saved by the installer.

To add another member later, repeat the same pattern with unused `-InstallDir` / `-Name` / `-TaskName` values and the
same `agent-harness-ci` label. To repair or remove a member, delete the runner in GitHub (**Settings > Actions >
Runners**), uninstall the matching scheduled task, and delete that member's install directory, then reinstall with
the snippet above if you are repairing it:

```powershell
Unregister-ScheduledTask -TaskName AgentHarness-GitHubRunner-CI-2 -Confirm:$false
Remove-Item -LiteralPath D:\Agents\github-runner-ci-2 -Recurse -Force
```

Replace `-2` with the member you are removing.

### Capacity (2026-09-20)

The tower has 28 logical cores and 31.8 GB RAM. `llama-server` (`ops/llama-server/run-qwen.ps1`, port 8090) uses
about 9 GB RSS when the model is loaded. Three concurrent CI jobs (separate `_work` checkouts) each run
`python -m pytest tests -q -n 8 --dist loadfile` (fixed workers, never `-n auto`; raised from `-n 4` after a
20-run soak median of 192 s stayed above two minutes) plus the live daemon still left about 8.6 GB free while all
three were in the test step. Historically a serial `python -m pytest tests -q` was about 5 minutes (302 s on
GitHub-hosted Windows; 426 s in the #132 soak on this Windows machine). Issue #132 soak on this Windows host
(782 passed, 4 skipped each run, no flakes): serial `-p no:xdist` 426 s; `-n 4 --dist loadfile` 20/20 green,
median 192 s, p90 194 s; `-n 8 --dist loadfile` 20/20 green, median 152 s, p90 156 s. The 1–2 minute band was
not reached at the worker cap of 8. The pool stays at **three** members. Local serial escape hatch:
`python -m pytest tests -q -p no:xdist`. Parallel-safety: Docker test networks are already unique per process;
listen ports use `port=0`; data dirs stay under `tmp_path`. No serial-only xdist marks were required.

### Cross-job isolation

Jobs must not share a checkout or venv (enforced by separate install dirs). Tests use `tmp_path` / `port=0` and do
not read `HARNESS_HOME` or the live daemon data dir. The live Docker networks `harness-sandbox` / `harness-egress`
belong to the production daemon; pytest uses per-job names `harness-test-sbx-<pid>-<id>` and
`harness-test-egress-<pid>-<id>`. Session containers are `harness-<10-hex-id>`. Remaining shared host resources that
are **read-only** or out of pytest control: the Docker engine itself and the `agent-harness-sandbox:py312` image tag.

## Tower runner

The pinned official runner is installed in `D:\Agents\github-runner`, with job work under its `_work` directory.
It is registered only to `dflippojr/agent-harness`, carries the custom
`agent-harness-tower` label, and starts at user logon through the hidden `AgentHarness-GitHubRunner` scheduled task.
Diagnostics live in `D:\Agents\github-runner\_diag`.

To repair or reinstall it, remove the runner in GitHub and its local install directory, obtain a fresh short-lived
registration token, then run:

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token
```

The token is never saved by the installer. GitHub stores the runner's own credential files in its install directory.

The live checkout must itself be on `main`; deployment intentionally fails closed on another branch or detached HEAD.
After first confirming that the checkout has no work that must be preserved, put it on `main` and fast-forward it:

```powershell
Set-Location D:\Projects\agent-harness
git status --short
git switch main
git fetch origin
git merge --ff-only origin/main
```

Do this as an explicit maintenance action, not from the Actions runner. Never discard local changes to make a deploy
pass.

## Review runner pool

Automated review (`.github/workflows/review.yml`) uses three repository-scoped self-hosted runners that share the
`agent-harness-review` label. Each member takes one job, so reviews of different PRs can run in parallel. They are
named, installed, and started as:

| GitHub name | Install dir | Hidden logon task |
| --- | --- | --- |
| `dflippotower-agent-harness-review-1` | `D:\Agents\github-runner-review-1` | `AgentHarness-GitHubRunner-Review-1` |
| `dflippotower-agent-harness-review-2` | `D:\Agents\github-runner-review-2` | `AgentHarness-GitHubRunner-Review-2` |
| `dflippotower-agent-harness-review-3` | `D:\Agents\github-runner-review-3` | `AgentHarness-GitHubRunner-Review-3` |

Use the existing parameterized installer (`ops/github/install-runner.ps1`). Do not add a pool wrapper. Obtain a fresh
short-lived registration token, then install one member at a time with distinct `-InstallDir` / `-Name` / `-TaskName`
and `-Labels agent-harness-review`:

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-1 `
  -Name dflippotower-agent-harness-review-1 `
  -TaskName AgentHarness-GitHubRunner-Review-1 `
  -Labels agent-harness-review
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-2 `
  -Name dflippotower-agent-harness-review-2 `
  -TaskName AgentHarness-GitHubRunner-Review-2 `
  -Labels agent-harness-review
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-3 `
  -Name dflippotower-agent-harness-review-3 `
  -TaskName AgentHarness-GitHubRunner-Review-3 `
  -Labels agent-harness-review
```

Each call consumes the token; request a new one for every member. The token is never saved by the installer.

To add a fourth member, repeat the same pattern with unused `-InstallDir` / `-Name` / `-TaskName` values and the same
label. To repair or remove a member, delete the runner in GitHub (**Settings > Actions > Runners**), uninstall the
matching scheduled task, and delete that member's install directory, then reinstall with the snippet above if you are
repairing it:

```powershell
Unregister-ScheduledTask -TaskName AgentHarness-GitHubRunner-Review-1 -Confirm:$false
Remove-Item -LiteralPath D:\Agents\github-runner-review-1 -Recurse -Force
```

Replace `-1` with the member you are removing. The same GitHub-delete + uninstall-task + delete-install-dir sequence
is the repair path used for the tower runner.

## Failure behavior

- CI failure, cancellation, pull-request run, or non-`main` run: no images and no deployment.
- Image build/push failure: no deployment. Actions cache export failure is ignored because the cache is optional.
- Docker unavailable, live checkout dirty, wrong branch, non-fast-forward history, or dependency resolution failure:
  deployment fails visibly before the daemon is stopped or the live checkout and virtual environment are changed.
- After the daemon is stopped, a swap, fast-forward, image retag, restart, or health-check failure triggers rollback:
  the partially deployed daemon is stopped, the previous checkout and untouched virtual environment are restored, and
  the previous daemon is started and health-checked. The Actions job still exits non-zero and reports whether rollback
  itself had any errors.
- A newer `main` commit superseding an older queued deployment makes the older deployment exit without changing the
  tower; the serialized newer workflow run handles it.
