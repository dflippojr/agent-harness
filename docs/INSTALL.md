# Installing Agent Harness

Run AI agents on Windows, Linux, or Apple Silicon macOS and drive them from a browser or your phone. Use either a
local model or your own hosted-provider CLI subscriptions. Agents work in Docker sandboxes and ask before risky
actions.

The installer sets up **Agent Harness Server**, which hosts the APIs and normally serves **Agent Harness Web**. An
**Agent Harness App** is a third-party API integration. **Agent Harness for Mac** installs the `harness` command
(**Agent Harness CLI**) and a **Mac Runner** together; the Python library in `sdk/` is **Agent Harness SDK**.

The full local-model profile supports Windows 10/11 and x86-64 Linux with an NVIDIA GPU. Apple Silicon macOS uses
the hosted-provider service profile (Claude, Codex, or Cursor) and does not download a local model.

## Optional Hub at daemon setup

After doctor passes, the daemon installer asks `Install the Hub admin console and link it to this daemon? [Y/n]`
when stdin is a terminal and no Hub flag was supplied. `--with-hub` / `--no-hub` (PowerShell: `-WithHub` / `-NoHub`)
decide without prompting. With redirected stdin, the default is no Hub. Every Hub action is also available through
`harness --help`; see [management-parity.md](management-parity.md). Add the Hub later by rerunning the daemon installer
with `--with-hub` / `-WithHub`. Web and other Apps keep their normal App pairing flow and never make this offer.

The Hub distribution is pending #546. Until a release exists, set `HARNESS_HUB_PACKAGE` to the released pip
distribution or `HARNESS_HUB_IMAGE` to the released image; there is no default placeholder package or image.
Use `--hub-package` / `--hub-image` (PowerShell: `-HubPackage` / `-HubImage`) to override those settings.
`--hub-method pip|docker` (`-HubMethod pip|docker`) selects the distribution. Auto uses pip and offers Docker when
running interactively with Docker and an image configured. Pip installs into a separate venv, leaving daemon
dependencies intact. Docker uses host networking to reach the loopback daemon; Docker Desktop must have host
networking enabled. On Unix the container runs as the installing user's UID/GID so its state remains removable.
The Hub persists its credentials in its own state directory, with no daemon secrets mounted.
That directory is owner-only (including inherited Windows ACLs) before the Hub starts.

The installer registers a per-user Hub service (systemd, launchd, or a Windows logon task), or a Docker container with
a restart policy. It shows the Hub's request ID and match code, runs `harness hub approve <request_id> --match <code>`
on this host, and waits for the Hub to redeem its claim. An existing claim is reported with `harness hub release
--confirm`; the installer never releases it automatically. Missing distributions, launch errors, and claim timeouts
fail the Hub step while leaving the daemon installed. A partially installed Hub remains recorded for cleanup.
`--no-start` / `-NoTasks` defers Hub setup too; start the daemon and rerun without that flag to install and link the Hub.
With startup disabled, doctor uses `--not-started` to check configuration, files, and Docker images while reporting
that live daemon and optional service probes remain to be run after startup. Adding the Hub later uses the preserved
daemon configuration's port.
GPU prerequisites, image files, and model checksums remain checked even when startup is disabled.

`--dry-run` / `-DryRun` prints these steps without prompting, installing, starting services, or approving claims.
Uninstall checks status and runs `harness hub release --confirm` before stopping the daemon. It removes only the Hub
service/container and dedicated venv/state recorded by this installer in `<InstallDir>/hub-install.json`; unrelated
Hub installs remain. A failed release stops uninstall. Uninstall also accepts `--dry-run` / `-DryRun`.
If the daemon is stopped and no installer-owned Hub is recorded, daemon uninstall can continue. A recorded Hub
requires starting the daemon first so its host-only release can complete.
An early failed install with no configuration and no installed Hub can also be cleaned up; an installed Hub still
requires restoring its daemon configuration before release.

**Distribution adapter contract for #546:** the pip module defaults to `harness_hub` (override with
`HARNESS_HUB_MODULE`); the image entrypoint and module accept `--daemon-url`, `--state-dir`, and `--claim-file`.
The Hub itself creates a PKCE claim, atomically publishes only `{"id":"pr-...","match_code":"123456"}` to the fresh
claim file, then redeems the approval and persists its credentials in `--state-dir`. It must reuse those credentials
on service restart. The file must contain no verifier, token, or host approval secret. This adapter is covered with
stub launches; the end-to-end installation test waits on the real release in #546.

## Requirements

| | Minimum | Tested |
| --- | --- | --- |
| GPU | Service: none. Full: NVIDIA, 12 GB VRAM, driver 580+ | RTX 4070 Ti Super 16 GB, driver 616.92 |
| RAM | 16 GB (32 GB for the Qwen model) | 32 GB DDR5 |
| Disk | ~20 GB (gpt-oss) or ~30 GB (Qwen) free | NVMe SSD for models |
| Software | Docker running, Git, curl, tar | Docker 29.7; Docker Desktop on Windows/macOS |

Linux local inference also needs
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
configured for Docker. The installer uses llama.cpp's pinned CUDA container, so a host CUDA compiler is not needed.

Models picked automatically:

- **16 GB VRAM and 32 GB RAM:** Qwen3.6-35B-A3B (Q4, 22 GB; experts partly in RAM). 64K context, ~65 tok/s.
- **12–15 GB VRAM:** gpt-oss-20b (MXFP4, 12 GB, fits on the GPU). 32K context.

Both are Apache 2.0.

## Hosted-provider service profile

The service profile installs Agent Harness Server, Agent Harness Web, the provider CLI image, and provider-specific
egress proxies.
It skips llama.cpp, model downloads, GPU checks, and every optional integration unless selected:

```powershell
powershell -ExecutionPolicy Bypass -File install\install.ps1 -Profile Service
ops\backends\login.ps1 claude  # repeat for codex or cursor as wanted
```

On Linux or macOS, use `install/install.sh --profile service` and then
`ops/backends/login.sh claude` (or `codex` / `cursor`). Credentials remain in provider-specific Docker volumes on
all platforms. Codex needs one more login per App that uses it (`login.ps1 codex -App <app id>`, or
`login.sh codex --app <app id>`): its credential can't be kept apart from its session history, which stays separate
per App (docs/phase8a-design.md, "Per-domain CLI state").

**Letting an App's end users use their own Claude plan (owner step, #365).** Hosting Claude Code for other people
"requires agreeing to our Commercial Terms of Service" (Anthropic's legal and compliance page). Before an App starts
Claude sign-ins for its end users, confirm: *I have accepted Anthropic's Commercial Terms of Service for hosting
Claude Code, and each end user signs in to the unmodified Claude Code with their own plan.* The harness does not
check this. The API is in `docs/app-api.md` ("End users' own logins").

Docker (Docker Desktop on Windows/macOS) and at least one provider login are the operational minimum. The installer configures Claude, Codex,
and Cursor adapters; an unused provider can remain logged out. Add modules with a PowerShell array, for example
`-EnableModules jobs,backup`. `endpoint`, `images`, `image_edit`, and `gpu_guard` automatically opt into `local_model` and restore
the GPU/model requirements. `image_edit` is a separate ~20 GB Qwen-Image-Edit download; ordinary installs and daemon
upgrades never fetch it. Those weights go in `images.models_dir` (default `C:/AI/comfy-models`, the same root as
Z-Image/quality). If that directory is missing, the installer uses `<InstallDir>/comfy-models` and writes
`images.models_dir` into the generated config so `python -m harness.doctor` and the daemon look in the same place.
The complete module catalog and security boundary are in
[`service-profile.md`](service-profile.md).

On an existing install, `-Profile Service` writes only `config\profile.yaml`; it preserves `harness.yaml`, local
overrides, paths, and secrets. Run with `-Profile Full` to restore the full profile. With no `-Profile`, upgrades
preserve the existing choice (and use Full for a new install).

## Install on Windows

```powershell
git clone https://github.com/dflippojr/agent-harness
cd agent-harness
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

On Linux/macOS, pull the checkout and rerun `install/install.sh` with the same options. Existing profile and config
are preserved when `--profile auto` (the default) is used.

No administrator rights are needed. The installer:

1. checks Windows, disk, Docker and Git, plus GPU/driver/RAM for a local model;
2. downloads [uv](https://github.com/astral-sh/uv) and creates a Python 3.12 environment;
3. for the full profile, downloads llama.cpp (build b10950, CUDA 13.3) and the model;
4. builds the sandbox image `agent-harness-sandbox:py312`;
5. writes a config to `%LOCALAPPDATA%\agent-harness\config`;
6. registers the Agent Harness Server logon task and, when enabled, the llama-server task, then starts them;
7. runs `python -m harness.doctor`.

Downloads resume if interrupted: run the installer again. Running it again later also repairs an install and keeps your
config (`-Force` rewrites it).

Useful options: `-Profile Service`, `-EnableModules jobs,backup`, `-Model gpt-oss`, `-InstallDir D:\agent-harness`, `-DataDir D:\agents`, `-ModelPath <existing .gguf>`,
`-LlamaDir <existing llama.cpp>`, `-Port 8100`, `-NoTasks`, `-DryRun`. See `Get-Help .\install\install.ps1 -Full`.

Then open **http://127.0.0.1:8100** in Agent Harness Web. The first model load takes a minute or two.

> **Antivirus:** some engines flag `uv.exe` (Astral's Rust-based Python manager) as suspicious because it's unsigned
> and downloads packages. It's a false positive; allow the `%LOCALAPPDATA%\agent-harness\bin` folder. You can check
> the file against [uv's GitHub release](https://github.com/astral-sh/uv/releases) with `gh attestation verify`.

## Install on Linux with NVIDIA

From an x86-64 Linux checkout:

```bash
git clone https://github.com/dflippojr/agent-harness
cd agent-harness
install/install.sh
```

The default is the full profile. The installer checks `nvidia-smi`, Docker GPU access, disk/RAM, and the Docker
engine; installs a pinned uv/Python environment; downloads the selected resumable GGUF; builds the sandbox; and
registers `systemd --user` services for llama.cpp and Agent Harness Server. llama.cpp runs from the pinned official
`server-cuda-b10830` image with the model mounted read-only and localhost port 8090 served through host networking.

If the Docker GPU check fails, install NVIDIA Container Toolkit and restart Docker before rerunning the installer.
On a headless host, an administrator may need to enable user lingering with `loginctl enable-linger <user>`.

Useful options include `--profile service`, `--enable-modules jobs,backup`, `--model gpt-oss`,
`--install-dir /srv/agent-harness`, `--data-dir /srv/agents`, `--model-path /models/model.gguf`,
`--existing-server http://127.0.0.1:8090`, `--no-start`, and `--dry-run`. Run `install/install.sh --help` for the
complete list. The installer is idempotent and does not overwrite base config unless `--force` is passed.

## Install on Apple Silicon macOS

Install and start Docker Desktop, then run:

```bash
git clone https://github.com/dflippojr/agent-harness
cd agent-harness
install/install.sh
ops/backends/login.sh codex  # or claude / cursor
```

macOS defaults to the service profile and rejects `full`, `local_model`, and modules that require a local model.
It builds the hosted-provider sandbox and egress proxies and registers a per-user launchd agent for Agent Harness
Server. No
Rosetta, local GPU model, or administrator privileges are required. Docker Desktop must be running before hosted
sessions can start.

## Check it

```powershell
cd agent-harness
& "$env:LOCALAPPDATA\agent-harness\venv\Scripts\python.exe" -m harness.doctor --config-dir "$env:LOCALAPPDATA\agent-harness\config" --instance Main
```

Linux/macOS:

```bash
~/.local/share/agent-harness/venv/bin/python -m harness.doctor \
  --config-dir ~/.local/share/agent-harness/config --instance Main
```

Logs are under `%LOCALAPPDATA%\agent-harness\logs` on Windows and
`~/.local/share/agent-harness/logs` on Unix (`daemon.log`, `llama-server.log`, and supervisor logs).

## Use Agent Harness Web from your phone

Agent Harness Server listens only on localhost. To reach Agent Harness Web from other devices, use
[Tailscale](https://tailscale.com):

1. Install Tailscale on the PC and your phone, and sign in to both with the same account.
2. In the Tailscale admin console, enable **HTTPS certificates** (DNS page) and Serve.
3. On the PC: `tailscale serve --bg --https=443 http://127.0.0.1:8100`
4. In `config\harness.yaml` (or `harness.local.yaml`) set `public_url: https://<pc-name>.<tailnet>.ts.net` and
   `allowed_logins: [you@example.com]`, then restart the Agent Harness Server task.
   To let a tailnet buddy look around for a couple of hours without owner powers, add them under `guests`
   in the untracked local file (`login` plus an ISO `until`), restart, and remove the entry when done.
   Guests can browse sessions, jobs and images; they cannot start tasks, approve, mint keys, or use GPU /
   Remote Control / Review. Default stays "this login is the owner."
   To add a household member, keep an explicit `allowed_logins` owner allowlist, then use **Actions → Accounts**
   (or `POST /api/admin/v1/accounts`) with their exact Tailscale login. Members see only their own work through
   `/api/v1`. Creating the first member while `allowed_logins` is empty fails closed.
   To let members use their own private GitHub repositories, configure `github_member_auth` and turn it on under
   **Actions → Accounts**; see [`member-github-auth.md`](member-github-auth.md).
   For a shared household device whose tailnet login is no single member, configure `google_signin` so members
   identify themselves with their linked Google account; see [`google-signin.md`](google-signin.md).
5. Open the URL on the phone, then choose **Share → Add to Home Screen**. The installed Agent Harness Web icon is
   labeled **Harness**. iOS may retain an older label until you remove that icon and add it again; no server or
   browser data migration is required. See [`web.md`](web.md).

Phone notifications (approvals with Approve/Deny buttons, task finished) use a self-hosted
[ntfy](https://ntfy.sh) server: see `docs/phase2-results.md` for the container and the `notify:` section.

## Install Agent Harness for Mac

After Tailscale and `public_url` are configured, add a `macbook` entry under `runners:` with an owner-side
`token_file`, restart Agent Harness Server, then choose **Settings → Apps → Pair Agent Harness for Mac** in Agent
Harness Web. Run the generated one-time command in Terminal on the Mac. It installs a venv, Agent Harness CLI and
Agent Harness SDK, and the sandboxed Mac Runner as a launchd agent—without SSH, sudo, or copying tokens. See
[`mac-client.md`](mac-client.md).

## What else you can turn on

Each is a section in `config\harness.yaml`, documented in the repository's `config/harness.yaml`:

| Feature | Section | Needs |
| --- | --- | --- |
| Projects backed by git repos, with review branches | `config\projects.yaml` | a local repo path |
| Pause for games / Plex transcodes | `gpu_guard` (on by default) | nothing |
| Web search for agents | `web` | SearXNG container (`docs/phase6b-results.md`) |
| OpenAI/Anthropic-compatible endpoint | `endpoint` (on by default) | a key from Settings → Inference endpoint |
| Image generation | `images` | ComfyUI portable + models in `images.models_dir` (`docs/phase6d-results.md`). Optional `quality-fast` needs the pinned Lightning LoRA in `models_dir/loras/` (`python -m harness.doctor` prints the filename, size, SHA-256, and path); optional `flux-fast` is installed with `ops/images-models.ps1` (`docs/flux-fast.md`). Optional `image_edit` stores Qwen-Image-Edit in that same `models_dir`. Optional Real-ESRGAN 2×/4× weights live under `images.upscale_dir` or `<comfy_dir>/ComfyUI/models/upscale_models`; generation still works without them. |
| Claude / Codex / Cursor as session backends | `backends` | `ops/backends/login.sh <backend>` on Unix or `login.ps1` on Windows (`docs/phase8a-design.md`) |
| Claude Code Remote Control from the phone | `remote_control` | Claude Code trusted in that project folder (`docs/phase8b-results.md`) |
| Memory library for agents | `memory_library` | clone URL in `harness.local.yaml` |
| Scheduled jobs | `jobs` (on by default) | nothing |
| Agent Harness Apps that start and drive sessions | always on | a token from Settings → Apps (`docs/app-api.md`) |
| Nightly backups | `backup` (on by default) | nothing |

## Optional Real-ESRGAN upscaling

Image generation does not upscale unless you ask. 2× and 4× use the upstream BSD-3-Clause general-image weights
(`RealESRGAN_x2plus`, `RealESRGAN_x4plus`; no face restoration or anime models). Put them in
`<comfy_dir>/ComfyUI/models/upscale_models` or set `images.upscale_dir`. `python -m harness.doctor` warns when they
are missing and prints the URLs plus SHA-256; ordinary Generate still works.

| File | URL | SHA-256 |
| --- | --- | --- |
| `RealESRGAN_x2plus.pth` | https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth | `49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb` |
| `RealESRGAN_x4plus.pth` | https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth | `4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1` |

Outputs above 36 million pixels are refused before allocation so a 16 GB GPU cannot host-OOM. License text:
`third_party/Real-ESRGAN.LICENSE`.

## Update

```powershell
cd agent-harness
git pull
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

## Switch the local model (tower)

`config/harness.yaml` lists two local models on the same llama-server (127.0.0.1:8090): `qwen3.6-35b-a3b` (the
default) and `qwen3.8-35b-a3b-distill` (empero-ai's distill, adopted in #413 from the
[#174 study](qwen38-distill-study.md); same speed, memory and tool-call reliability). The supervisor
`ops/llama-server/run-qwen.ps1` loads whichever one is `default_model`, re-reading the config before each server start,
so the switch is one line in the untracked `config/harness.local.yaml`:

```yaml
default_model: qwen3.8-35b-a3b-distill
```

The file must already be at `C:/AI/models/Qwen3.8-35B-A3B-Q4_K_M.gguf`: revision
`b1f9d1dcc3de8aa867669b0ab919384aeeb9b8d5` of `empero-ai/Qwen3.8-35B-A3B-Distill-GGUF`, 21,713,462,944 bytes, SHA-256
`196103269085bc54c9b8f49ed21e9f53e1b56b465e8b796c6d8e31e06f63cfa5`. If it is missing, the supervisor logs it and
serves Qwen3.6 instead. Both models load with `--load-mode none` (#405).

To apply it, unload the running model and restart the daemon so both sides pick up the line:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8100/gpu/unload
powershell -ExecutionPolicy Bypass -File ops\harness\restart-daemon.ps1
```

The next turn (or Actions -> Load local model now) loads the new model; `C:\AI\logs\llama-server-supervisor.log`
names it on each start. **Rollback:** delete the line (or set `default_model: qwen3.6-35b-a3b`) and run the same two
commands. Leave Settings -> Backends -> local on the default: a different pick there changes the name the harness
sends, not the file the server loads.

## Backups and restore

With the `backup` module on (the default), the daemon writes `backup.dir/<YYYY-MM-DD>/` every night at `backup.at`
and deletes dated folders older than `backup.keep_days`. `harness maintenance backup` takes one now.

A backup folder holds exactly:

| File | What it is |
| --- | --- |
| `harness.sqlite3` | the main store: App registry, accounts, settings, jobs, skills, provider credentials, audit log |
| `apps/<app_id>.sqlite3` | one per App store, Agent Harness Web's `app-web.sqlite3` included (every owner and member session) |
| `transcripts.zip` | the owner's transcripts (`<data_dir>/transcripts`) |
| `transcripts/users/<user_id>.zip` | each member's transcripts, when they have any |
| `transcripts/apps/<app_id>.zip` | each App's transcripts, when it has any |
| `config/` | `harness.yaml`, `harness.local.yaml`, `projects.yaml` from the install's `config` folder |
| `managed-config*.json` | the managed-config overlay from `<data_dir>` |
| `config-audit.jsonl` | the configuration audit trail (who changed which setting), when `<data_dir>` has one |

It does not hold workspaces (a local git project's branches are already saved in its source repository),
checkpoints, artifact files, `pre-migration/` snapshots, logs, image archives, model files or anything off this
machine. Copy the backup folder elsewhere yourself if you want an off-machine copy.

**Configuration audit trail (#469).** The backup copies a stable prefix of `<data_dir>/config-audit.jsonl`: the file's
length is captured first and only complete lines inside it are kept, so a record appended (or half-written) during the
copy belongs to the next backup. It is not a cross-file transaction with the database and overlay snapshots, and it is
not tamper evidence. A missing file is normal; a symlink, an unreadable file or a line that isn't a UTF-8 JSON object
fails the backup (the message never quotes record content). The active trail has no rotation, and snapshots follow
`backup.keep_days` like the rest of the dated folder. The SQLite audit history restores with the database snapshot.
Review is owner-only and local: read the active file or a restored snapshot.

`member-keys.key` is copied separately (#414), with owner-only permissions (a protected owner-only ACL on Windows,
mode 600 elsewhere). Set `backup.member_key_dir` in local YAML to choose its directory; unset defaults to
`backup.dir/member-keys/`, next to and outside the dated database folders. Each copy is named `<SHA-256>.key`, and
the database snapshot records the fingerprint it expects. Key copies are retained even when dated backups are
pruned, so an older off-site database can still find its key after a key change. Move obsolete copies manually
only when no retained database needs them. The key directory cannot be inside a dated backup folder.
An invalid or unreadable source key, unavailable key directory or permission-setting failure skips the key copy
with a warning and leaves the database backup usable. A key is never written without first restricting access.

**Warning:** the key and a database backup together decrypt every member's stored API key. Every run that copies
the key logs this warning. Keep the key copy somewhere other than where the database backups are kept off-site;
the default provides local separation only. Copy the key directory separately when taking backups off-site.

Check a backup at any time; it changes nothing and can run while the daemon is up:

```powershell
python -m harness_modules.backup.restore verify <backup.dir>\2026-10-04
```

It fails (exit code 1) if a store doesn't open read-only or fails `PRAGMA integrity_check`, if a store's schema is
newer than this code (upgrade the harness first), if a zip fails its CRC check or holds a path outside its folder,
or if an App or member file isn't named by a valid id.

To restore, stop the daemon (the scheduled task or service), then:

```powershell
python -m harness_modules.backup.restore restore <backup.dir>\2026-10-04          # dry run: prints what it would replace
python -m harness_modules.backup.restore restore <backup.dir>\2026-10-04 --apply  # does it
```

Add `--config-dir <folder>` if the install doesn't use the default `config` folder. `restore` verifies the backup
first and refuses, changing nothing, if verification fails or the daemon is still running (it answers on the
configured port, or something holds a store's write lock). It restores into the configured `data_dir`:

- the main store, and every App store in the backup;
- `member-keys.key` from the separate key directory, only when its fingerprint matches the database snapshot.
  For an off-site restore, set `backup.member_key_dir` to the separately retrieved key directory. Missing,
  unreadable, invalid or mismatched copies produce a warning and the database restore still succeeds; members
  must re-add their API keys if the existing key cannot decrypt them. Older backups without a fingerprint also
  warn and leave the existing key in place;
- the owner's, members' and Apps' transcripts. Each transcripts folder is replaced as a whole;
- the configuration audit snapshot, with or without `--include-config` (it is history, not configuration). It is
  always archived at `<data_dir>/restored-audits/<sha256>/config-audit.jsonl`; if no active `config-audit.jsonl`
  exists it is also restored there, otherwise the active file's bytes are left untouched. Records are never merged
  or deduplicated. Re-running the same restore is a no-op for the archive; an existing archive with different bytes
  refuses the restore. Restoring an older backup therefore rolls the *active* trail back only when none existed, and
  archives (never deletes) newer history; archives are not pruned by `backup.keep_days`;
- with `--include-config` only: the `config` files and the managed-config overlay. They are left out by default
  because they may hold another machine's paths.

An App store or App transcript archive whose App no longer exists in the restored main store (erased after its
revoke) is skipped with a warning, so a restore can't bring back erased App data on its own.

Nothing is deleted. `--apply` first moves every file and folder it replaces, a store's `-wal` and `-shm` files
included, into `<data_dir>/restore-<timestamp>-previous/`, keeping their paths relative to `data_dir` (config files
go under its `config/`). `RESTORE.txt` in that folder lists what was moved and what was put in place. To undo, stop
the daemon, move the restored files listed there out of the way and move the folder's contents back. Start the
daemon after a restore as usual; `python -m harness.doctor` checks the result.
Doctor also reports whether the last backup has a separate member-key copy and repeats the off-site warning.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File install\uninstall.ps1              # tasks and processes; keeps data and models
powershell -ExecutionPolicy Bypass -File install\uninstall.ps1 -RemoveFiles # also deletes %LOCALAPPDATA%\agent-harness
```

Linux/macOS equivalents:

```bash
install/uninstall.sh                 # remove per-user services; keep data
install/uninstall.sh --remove-files  # also remove the install directory
```

## Security model, briefly

- One owner per install, plus optional owner-provisioned household members and time-boxed guests. Agent Harness Web
  and the APIs treat a local caller as the owner only when it sends the local owner token (see
  [Local callers](#local-callers)); other devices need a tailnet login plus, for the inference endpoint and
  App API, a key or token. Members authenticate only with the exact `Tailscale-User-Login` the owner stored.
  Optional `guests:` entries grant time-boxed read-only Agent Harness Web access to a named tailnet login without
  owner or member powers. Member data lives under `data_dir/users/<opaque-id>/`. The machine owner remains
  inside the host/OS trust boundary and can read local storage; household isolation prevents accidental or
  API/UI cross-account access, not a hostile administrator. Optional member GitHub sign-in
  ([`member-github-auth.md`](member-github-auth.md)) keeps each member's credential in the OS secure store under
  their own Git Credential Manager namespace, inside the same host trust boundary.
- Agents are untrusted: shell commands run in a Docker container with only the workspace mounted and no network
  unless you approve it. Pushes, deletes outside scratch paths, and network commands ask first.
- Web fetches refuse private, tailnet and metadata addresses. Agent Harness App-provided context and web pages are marked as
  information, not instructions.

### Local callers

`tailscale serve` adds the caller's tailnet login to every request it forwards. Any other program on the machine, or
a container that reaches the host's loopback, can send the same `Tailscale-*` headers, so the daemon trusts them only
when tailscaled is the other end of the connection: it looks the connection up in the OS connection table and checks
that the owning process is `tailscaled.exe` in the Tailscale install directory (`%ProgramFiles%\Tailscale`). From any
other process, or when the lookup fails, the daemon removes those headers before it reads them. A request without a
login from tailscaled reached the daemon's loopback listener directly, so it must say who it is:

- The owner's own tools send the **local owner token** in the `X-Agent-Harness-Local-Token` header. The daemon
  creates it on first start at `data_dir/local-owner.token` and keeps it across restarts; keep `data_dir` readable
  only by the daemon's account. `python -m harness.cli` reads it automatically when it talks to `127.0.0.1` or `localhost`; other
  scripts can read the file or take it from `HARNESS_LOCAL_TOKEN`. Delete the file and restart the daemon to rotate it.
- An App, device or owner API token (`Authorization: Bearer …`) on `/api/v1` and `/api/admin/v1`, an owner admin
  token anywhere, an inference key on `/v1`, a runner token on the runner routes, or a stream ticket still works; its
  route checks it as before.
- `/health` and `/metrics` answer without a credential, so Prometheus and the restart scripts keep working.

Approving, denying or releasing the standalone Hub's claim needs one more secret: the daemon writes a new
`data_dir/hub-approval.secret` at every start, and `harness hub approve`, `deny` and `release` read it, so they work only
on the daemon host as the daemon's account ([admin-api.md](admin-api.md#hub-claim)).

Everything else gets **401**. A browser on the server itself should use the tailnet URL rather than
`http://127.0.0.1:8100`.

The tailscaled check is implemented on Windows. On other platforms the daemon ignores the `Tailscale-*` headers, so
tailnet devices get **401** unless you set `listen.trust_unverified_identity_headers: true` in `harness.local.yaml`.
That setting is **unsafe**: it trusts those headers from every local process and every container that can reach the
host's loopback, so set it only on a single-user machine that runs no agent sandboxes with network access.
