# GitHub CI and container images

`.github/workflows/ci-cd.yml` keeps untrusted CI away from the tower:

1. **test** runs the complete test suite and checks `harness/web/app.js` on GitHub-hosted Windows for pull requests,
   pushes to `main`, and manual workflow runs.
2. **publish-images** runs only after a tested push to `main`. GitHub-hosted Linux builders publish both runtime
   images to GHCR:
   - `ghcr.io/dflippojr/agent-harness-sandbox:sha-<commit>` and `:py312`
   - `ghcr.io/dflippojr/agent-harness-cli:sha-<commit>` and `:1`
3. **deploy-tower** runs only after image publication for a trusted `main` push. The repository-scoped runner pulls
   the immutable images, verifies that the live checkout is clean `main` and can fast-forward, installs requirements,
   retags the images to the daemon's local names, restarts the daemon, and waits for `/health`.

The SHA tags are immutable deployment inputs; the stable tags match the local names already expected by the daemon.
The CLI Dockerfile accepts `SANDBOX_IMAGE` so its hosted build is based on the exact sandbox image from the same commit.

## Deployment boundary

The daemon itself is deliberately not containerized. It is a host Python process because it coordinates Windows
scheduled tasks, local repository paths, Docker sandbox creation, GPU/model controls, and native provider logins.

The owner explicitly approved the repository-scoped self-hosted runner. It gives trusted `main` workflow code the
tower user's filesystem, Docker, credentials, and service-restart authority. Pull requests never target it; all
third-party actions are pinned to exact commits, main deployments serialize rather than being canceled midway, and
the GitHub `tower-production` environment accepts deployments from `main` only.

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

## Failure behavior

- Test failure: no images and no deployment.
- Image build/push failure: no deployment.
- Docker unavailable, live checkout dirty, wrong branch, non-fast-forward history, dependency install failure, or failed
  daemon health check: deployment fails visibly in Actions rather than forcing or discarding host state.
- A newer `main` commit superseding an older queued deployment makes the older deployment exit without changing the
  tower; the serialized newer workflow run handles it.
