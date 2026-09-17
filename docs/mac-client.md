# Mac client and runner

The Mac package installs the `harness` CLI and the outbound tool runner together. It does not need a repository
clone, SSH, sudo, or manual token copying.

## Install or update

1. Configure the daemon's `public_url` and a `runners.macbook.token_file`, then restart it.
2. In Control Center, open **Settings → Apps → Pair Mac client** and create an install command.
3. Run that command in Terminal on the Mac while its 10-minute code is valid.

The script comes from the daemon, downloads a version-matched runner bundle from the same daemon, and installs:

- `~/.agent-harness/venv` — Python environment with the CLI dependency;
- `~/.local/bin/harness` — command wrapper (add this directory to `PATH` if prompted);
- `~/.agent-harness/client/harness_client.py` — the supported Python SDK, importable from the installed venv;
- `~/.agent-harness/client/config.json` — daemon URL and non-browser owner token, mode 0600;
- `~/.agent-harness/runner/config.json` — runner URL, name, token, and allowed project roots, mode 0600;
- `~/Library/LaunchAgents/dev.agent-harness.runner.plist` — runner started at login; and
- `~/.agent-harness/logs/runner.log` plus per-session workspaces.

The code works once. Redemption mints a revocable owner client entry shown in Settings and returns the runner's
configured token only in the no-store response. The code is hashed in SQLite; the runner token remains in the
daemon's owner-managed file and the Mac's mode-0600 config. Re-running a newly generated command updates the CLI and
runner while keeping the same layout.

After the first pairing, update without minting another credential by rerunning the installer without `--code`:

```bash
curl -fsSL https://<daemon>/mac-client/install.sh | bash -s -- --server https://<daemon>
```

It preserves both mode-0600 config files and restarts the updated launchd agent.

## Use

```bash
harness new "Fix the failing test" --project my-mac-project
harness list
harness watch <session-id>
harness approve <session-id> [approval-id]

harness projects add ~/Projects/my-repo
harness runner status
harness runner restart
harness runner logs --follow
```

`projects add` changes only the Mac runner's local repository-root allowlist and restarts it. It does not create or
edit the tower's project catalog; add a matching `target: macbook` project to the daemon configuration separately.
Shell commands continue to use the existing `sandbox-exec` profile, repository paths must be under an allowed root,
and approvals use the same daemon flow as the Control Center.

The older `ops/macbook/deploy.ps1` SSH deployment remains available for repair and development. It now installs the
same venv, CLI, and launchd layout, but native pairing is the normal first-install path.
