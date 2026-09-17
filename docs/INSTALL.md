# Installing the agent harness

Run AI agents on Windows, Linux, or Apple Silicon macOS and drive them from a browser or your phone. Use either a
local model or your own hosted-provider CLI subscriptions. Agents work in Docker sandboxes and ask before risky
actions.

The full local-model profile supports Windows 10/11 and x86-64 Linux with an NVIDIA GPU. Apple Silicon macOS uses
the hosted-provider service profile (Claude, Codex, or Cursor) and does not download a local model.

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

The service profile installs the daemon, Control Center, provider CLI image, and provider-specific egress proxies.
It skips llama.cpp, model downloads, GPU checks, and every optional integration unless selected:

```powershell
powershell -ExecutionPolicy Bypass -File install\install.ps1 -Profile Service
ops\backends\login.ps1 claude  # repeat for codex or cursor as wanted
```

On Linux or macOS, use `install/install.sh --profile service` and then
`ops/backends/login.sh claude` (or `codex` / `cursor`). Credentials remain in provider-specific Docker volumes on
all platforms.

Docker (Docker Desktop on Windows/macOS) and at least one provider login are the operational minimum. The installer configures Claude, Codex,
and Cursor adapters; an unused provider can remain logged out. Add modules with a PowerShell array, for example
`-EnableModules jobs,backup`. `endpoint`, `images`, and `gpu_guard` automatically opt into `local_model` and restore
the GPU/model requirements. The complete module catalog and security boundary are in
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
6. registers the daemon logon task and, when enabled, the llama-server task, then starts them;
7. runs `python -m harness.doctor`.

Downloads resume if interrupted: run the installer again. Running it again later also repairs an install and keeps your
config (`-Force` rewrites it).

Useful options: `-Profile Service`, `-EnableModules jobs,backup`, `-Model gpt-oss`, `-InstallDir D:\agent-harness`, `-DataDir D:\agents`, `-ModelPath <existing .gguf>`,
`-LlamaDir <existing llama.cpp>`, `-Port 8100`, `-NoTasks`, `-DryRun`. See `Get-Help .\install\install.ps1 -Full`.

Then open **http://127.0.0.1:8100**. The first model load takes a minute or two.

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
registers `systemd --user` services for llama.cpp and the daemon. llama.cpp runs from the pinned official
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
It builds the hosted-provider sandbox and egress proxies and registers a per-user launchd agent for the daemon. No
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

## Use it from your phone

The daemon only listens on localhost. To reach it from other devices, use [Tailscale](https://tailscale.com):

1. Install Tailscale on the PC and your phone, and sign in to both with the same account.
2. In the Tailscale admin console, enable **HTTPS certificates** (DNS page) and Serve.
3. On the PC: `tailscale serve --bg --https=443 http://127.0.0.1:8100`
4. In `config\harness.yaml` (or `harness.local.yaml`) set `public_url: https://<pc-name>.<tailnet>.ts.net` and
   `allowed_logins: [you@example.com]`, then restart the daemon task.
   To let a tailnet buddy look around for a couple of hours without owner powers, add them under `guests`
   in the untracked local file (`login` plus an ISO `until`), restart, and remove the entry when done.
   Guests can browse sessions, jobs and images; they cannot start tasks, approve, mint keys, or use GPU /
   Remote Control / Review. Default stays "this login is the owner."
   To add a household member, keep an explicit `allowed_logins` owner allowlist, then use **Settings → Accounts**
   (or `POST /api/admin/v1/accounts`) with their exact Tailscale login. Members see only their own work through
   `/api/v1`. Creating the first member while `allowed_logins` is empty fails closed.
5. Open the URL on the phone, then Share → Add to Home Screen.

Phone notifications (approvals with Approve/Deny buttons, task finished) use a self-hosted
[ntfy](https://ntfy.sh) server: see `docs/phase2-results.md` for the container and the `notify:` section.

## Add a Mac runner and CLI

After Tailscale and `public_url` are configured, add a `macbook` entry under `runners:` with an owner-side
`token_file`, restart the daemon, then choose **Settings → Apps → Pair Mac client**. Run the generated one-time
command in Terminal on the Mac. It installs a venv, the `harness` CLI and Python SDK, and the sandboxed outbound
runner as a launchd agent—without SSH, sudo, or copying tokens. See [`mac-client.md`](mac-client.md).

## What else you can turn on

Each is a section in `config\harness.yaml`, documented in the repository's `config/harness.yaml`:

| Feature | Section | Needs |
| --- | --- | --- |
| Projects backed by git repos, with review branches | `config\projects.yaml` | a local repo path |
| Pause for games / Plex transcodes | `gpu_guard` (on by default) | nothing |
| Web search for agents | `web` | SearXNG container (`docs/phase6b-results.md`) |
| OpenAI/Anthropic-compatible endpoint | `endpoint` (on by default) | a key from Settings → Inference endpoint |
| Image generation | `images` | ComfyUI portable + models (`docs/phase6d-results.md`) |
| Claude / Codex / Cursor as session backends | `backends` | `ops/backends/login.sh <backend>` on Unix or `login.ps1` on Windows (`docs/phase8a-design.md`) |
| Claude Code Remote Control from the phone | `remote_control` | Claude Code trusted in that project folder (`docs/phase8b-results.md`) |
| Memory library for agents | `memory_library` | clone URL in `harness.local.yaml` |
| Scheduled jobs | `jobs` (on by default) | nothing |
| Apps that start and drive sessions | always on | a token from Settings → Apps (`docs/app-api.md`) |
| Nightly backups | `backup` (on by default) | nothing |

## Update

```powershell
cd agent-harness
git pull
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

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

- One owner per install, plus optional owner-provisioned household members and time-boxed guests. The web app
  and APIs trust localhost as the owner; other devices need a tailnet login plus, for the inference endpoint and
  app API, a key or token. Members authenticate only with the exact `Tailscale-User-Login` the owner stored.
  Optional `guests:` entries grant time-boxed read-only Control Center access to a named tailnet login without
  owner or member powers. Member data lives under `data_dir/users/<opaque-id>/`. The machine owner remains
  inside the host/OS trust boundary and can read local storage; household isolation prevents accidental or
  API/UI cross-account access, not a hostile administrator.
- Agents are untrusted: shell commands run in a Docker container with only the workspace mounted and no network
  unless you approve it. Pushes, deletes outside scratch paths, and network commands ask first.
- Web fetches refuse private, tailnet and metadata addresses. App-provided context and web pages are marked as
  information, not instructions.
