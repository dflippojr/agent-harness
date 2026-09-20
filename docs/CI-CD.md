# GitHub CI and container images

`.github/workflows/ci.yml` is the single test workflow, and `.github/workflows/ci-cd.yml` publishes and deploys only
after that workflow succeeds for the exact commit pushed to `main`:

1. **CI / test** runs the complete test suite on the dedicated repository-scoped `agent-harness-ci` runner for pull
   requests and pushes to `main`. There is no duplicate `windows-latest` test job in the image/deployment workflow.
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
reserved for deployment; tests use `agent-harness-ci`, SonarCloud uses GitHub-hosted Windows, and automated review uses
the `agent-harness-review` pool.

`.github/workflows/sonar.yml` installs the test extras, runs `pytest` with `pytest-cov` Cobertura output
(`coverage.xml`) on `windows-latest`, then scans with `sonar.qualitygate.wait=true`. The suite is Windows-native;
Ubuntu hosted runners fail coverage collection on `msvcrt`, console-creation flags, and Windows paths. Docker-image
and GPU tests still skip on the hosted runner and do not contribute coverage. Non-Python trees (`harness/web`, `ops`,
scripts that are not `.py`) are excluded from the coverage metric. Confirm the SonarCloud project still uses a gate
that includes coverage on new code; the workflow cannot set that condition itself.

`workflow_run` executes the workflow file from the default branch and can access secrets, so its jobs reject every
event except a successful `CI` run caused by a push whose head branch is `main`. They check out and deploy only the
reported `head_sha`; they never check out or execute pull-request code. Third-party actions are pinned to exact
commits, main deployments serialize rather than being canceled midway, and the GitHub `tower-production` environment
accepts deployments from `main` only.

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
