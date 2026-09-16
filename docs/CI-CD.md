# GitHub CI and container images

`.github/workflows/ci-cd.yml` keeps untrusted CI away from the tower:

1. **test** runs the complete test suite and checks `harness/web/app.js` on GitHub-hosted Windows for pull requests,
   pushes to `main`, and manual workflow runs.
2. **publish-images** runs only after a tested push to `main`. GitHub-hosted Linux builders publish both runtime
   images to GHCR:
   - `ghcr.io/dflippojr/agent-harness-sandbox:sha-<commit>` and `:py312`
   - `ghcr.io/dflippojr/agent-harness-cli:sha-<commit>` and `:1`

The SHA tags are immutable deployment inputs; the stable tags match the local names already expected by the daemon.
The CLI Dockerfile accepts `SANDBOX_IMAGE` so its hosted build is based on the exact sandbox image from the same commit.

## Deployment boundary

The daemon itself is deliberately not containerized. It is a host Python process because it coordinates Windows
scheduled tasks, local repository paths, Docker sandbox creation, GPU/model controls, and native provider logins.

Automatic tower deployment is not enabled by this workflow. A repository-scoped self-hosted runner would give trusted
`main` workflow code the tower user's filesystem, Docker, credentials, and service-restart authority. That is useful,
but it is a separate security decision from hosted CI/image publishing and should be enabled only with explicit owner
approval. Pull requests must never target such a runner.

Until then, the existing manual update path remains:

```powershell
git pull --ff-only
docker pull ghcr.io/dflippojr/agent-harness-sandbox:py312
docker pull ghcr.io/dflippojr/agent-harness-cli:1
docker tag ghcr.io/dflippojr/agent-harness-sandbox:py312 agent-harness-sandbox:py312
docker tag ghcr.io/dflippojr/agent-harness-cli:1 agent-harness-cli:1
.\ops\harness\restart-daemon.ps1
```
