# Agent Harness for Mac

Agent Harness for Mac installs **Agent Harness CLI** (the `harness` command) and the outbound **Mac Runner** together.
The installed Python library is **Agent Harness SDK**. The distribution does not need a repository clone, SSH,
sudo, or manual token copying.

## Install or update

1. Configure Agent Harness Server's `public_url` and a `runners.macbook.token_file`, then restart the Server.
2. In Agent Harness Web, open **Settings → Apps → Pair Agent Harness for Mac** and create an install command.
3. Run that command in Terminal on the Mac while its 10-minute code is valid.

The script comes from Agent Harness Server, downloads a version-matched bundle from the same Server, and installs:

- `~/.agent-harness/venv` — Python environment with the CLI dependency;
- `~/.local/bin/harness` — command wrapper (add this directory to `PATH` if prompted);
- `~/.agent-harness/client/harness_client.py` — Agent Harness SDK, importable from the installed venv;
- `~/.agent-harness/client/config.json` — Agent Harness Server URL and non-browser owner token, mode 0600;
- `~/.agent-harness/runner/config.json` — Mac Runner URL, name, token, and allowed project roots, mode 0600;
- `~/Library/LaunchAgents/dev.agent-harness.runner.plist` — runner started at login; and
- `~/.agent-harness/logs/runner.log` plus per-session workspaces.

The code works once. Redemption mints a revocable CLI connection shown in Settings and returns the Mac Runner's
configured token only in the no-store response. The code is hashed in SQLite; the runner token remains in the
Server's owner-managed file and the Mac's mode-0600 config. Re-running a newly generated command updates Agent
Harness CLI and the Mac Runner while keeping the same layout.

After the first pairing, update without minting another credential by rerunning the installer without `--code`:

```bash
curl -fsSL https://<server>/mac-client/install.sh | bash -s -- --server https://<server>
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
edit the tower's project catalog; add a matching `target: macbook` project to Agent Harness Server configuration separately.
Shell commands continue to use the existing `sandbox-exec` profile, repository paths must be under an allowed root,
and approvals use the same Agent Harness Server flow as Agent Harness Web.

The older `ops/macbook/deploy.ps1` SSH deployment remains available for repair and development. It installs the same
venv, Agent Harness CLI, and launchd layout, but native pairing is the normal first-install path.
