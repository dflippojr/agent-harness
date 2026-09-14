# Phase 3: tower projects and homelab tasks

Built 2026-09-14 against the always-on Qwen3.6-35B-A3B server (64K context), memory now at DDR5-5600 (XMP on).

## What exists

| Piece | Where | Notes |
| --- | --- | --- |
| Project registry | `config/projects.yaml` | `repo` (local path or URL), `base_branch`, `homelab`, `quota_mb` |
| Session branches and review | `harness/projects.py`, `Manager.review` | host-side git only; `POST /sessions/{id}/review/{merge,push,discard}` |
| Homelab tools | `harness/homelab.py`, `homelab:` in `config/harness.yaml` | services, logs, config files, PromQL, approval-gated restart |
| Cleanup and quotas | `harness/maintenance.py`, `cleanup:` in `config/harness.yaml` | hourly loop, `GET /maintenance`, `POST /maintenance/cleanup` |
| Web app | `harness/web/app.js` | Review card on the Changes tab, Disk card in Settings, homelab tool/approval rendering |
| Restart helper | `ops/harness/restart-daemon.ps1` | `Stop-ScheduledTask` leaves the old daemon holding the port |
| Demo service | `D:/Docker/harness-demo` | disposable container for exercising the restart flow |
| Tests | `tests/test_phase3.py` | 11 tests (39 total), no Docker or GPU needed |

## Git projects

- A session in a `repo` project gets the repo cloned **host-side** into its workspace on branch `agent/<session>`
  (on its first run, before it takes the GPU slot). The system prompt tells the agent which branch it's on and not to
  push; `git push` in the sandbox is denied for these projects.
- Every run starts with a host-side `git fetch origin`, so `origin/<base>` is current if the user asks the agent to
  catch up.
- At the end of every run the daemon commits anything left uncommitted ("Uncommitted work at the end of a run") and,
  for a **local** source, fetches the branch into that repo as `agent/<session>`. The work is then reviewable there
  and survives workspace cleanup.
- **Merge** (local sources) squash-merges into the base branch with the session title as the subject and the
  agent's commit subjects in the body, then deletes the branch. A checked-out source must be on the base branch with
  nothing staged; unrelated unstaged edits are fine (git refuses if the merge would touch them). Conflicts are
  rolled back with `git reset --merge` and reported with the file names. A bare source is merged in a temporary
  worktree.
- **Push** (URL sources) pushes the branch with the tower's git credentials (Git Credential Manager). The sandbox
  never sees credentials. URL sources are never written to without this explicit action. The tower has no GitHub
  login yet, so private-repo push is untested beyond a `file://` remote.
- **Discard** deletes the branch, the container, and the workspace.
- Line endings: Git for Windows sets `core.autocrlf=true` system-wide. Workspaces are cloned with
  `core.autocrlf=false` (LF for the Linux sandbox), but git commands in a source repo must not override the setting,
  or every CRLF file there looks modified and merges are refused. A first version did that; the tests caught it.

## Homelab tools

Only for projects with `homelab: true`. They run in the daemon; the sandbox still can't reach Docker or services.

| Tool | Policy | What it does |
| --- | --- | --- |
| `homelab_services` | allow | state, health, exit code, OOM flag, restarts, restart policy, image per allowlisted service; no env vars or mounts |
| `container_logs` | allow | `docker logs --timestamps`, tail ≤ 2000, validated `since` |
| `read_service_config` | allow | files under managed stacks in `D:/Docker`; `secrets/`, `data/`, `.git`, `.env`, tokens, keys, `*password*` refused |
| `prometheus_query` | allow | instant or range PromQL, summarized per series |
| `restart_service` | **ask** | `docker restart`, or `docker compose up -d --no-build <service>` when the container is gone |

Allowlist: plex-webhook, prometheus, grafana, cadvisor, ntfy, harness-demo. Portainer and sandboxes are not on it.

## Cleanup and quotas

- Hourly (first pass 2 minutes after start): remove stopped containers of sessions finished more than 24 h ago
  (a follow-up message recreates one; anything installed in the old one is lost), delete workspaces of sessions
  finished more than 14 days ago, and delete workspace directories with no session. Before deleting a local-repo
  workspace the branch is saved to the source again; a URL-repo workspace with unpushed commits is kept and reported.
  A removed workspace makes the session read-only (continue with "Run again").
- Workspace quota 5 GB (per-project `quota_mb`): checked after `run_shell`, `git_clone`, and `write_file`, at most
  every 30 s until the workspace is past 80% of quota. Growing past the quota stops the run (`quota_exceeded`);
  shrinking is always allowed, so a follow-up "delete the build artifacts" works.
- New sessions are refused (HTTP 507) when the data drive has under 20 GB free.
- Not covered: the container's writable layer (e.g. packages installed outside /workspace). Docker Desktop's overlay
  storage has no per-container size limit; `GET /maintenance` reports each container's size instead.

## Verification on the tower

1. **Homelab (session `7b459257a4`):** `docker stop harness-demo`, then from the API (as the phone does): "Something
   on the homelab seems down. Check the services, figure out what happened from the logs, and restart whatever needs
   it." Qwen listed services, read logs, found `harness-demo` exited 0 after SIGTERM, and requested
   `restart_service harness-demo` (the only approval). After approval it verified the container was up and reported
   all other services healthy. One small reasoning slip: it attributed the missing auto-restart to exit code 0
   instead of the manual stop.
2. **Project branch (session `b60745eb38`):** project `invoice-tools`, "The test suite is failing. Find the bug, fix
   it, run the tests, and commit the fix." Qwen fixed `>` → `>=`, ran tests (4 pass), committed; the branch
   `agent/b60745eb38` appeared in `D:/Agents/repos/invoice-tools` with `main` untouched, and the Changes tab shows the
   Review card with the one-line diff. **Not merged** (merging would remove the bug the demo templates rely on).
3. Live `POST /maintenance/cleanup` parsed the real sandbox containers and removed nothing (all sessions are recent).
4. Phone-size screenshots of the Review card, Settings Disk card, and the homelab session (`runs/shots`).

## Phone exit test (user)

1. `docker stop harness-demo` (or ask for it). From the iPhone: New task → project **homelab** → "Something on the
   homelab seems down; diagnose it from the logs and restart it." Approve the restart from the notification.
2. From the iPhone: open session `b60745eb38` → Changes → **Merge into main** (or Discard), or start a fresh
   `invoice-tools` / `plex-webhook` task and review that branch.

## Known gaps / next

- Merge conflicts can only be fixed by asking the agent to merge `origin/<base>`; there's no conflict UI.
- No GitHub credentials on the tower yet, so URL projects can't push to private repos until `git credential-manager`
  is signed in (daemon-side only).
- Homelab tools can't restart the daemon's own dependencies safely in-flight (restarting ntfy mid-approval drops that
  notification; the approval card in the app still works).
