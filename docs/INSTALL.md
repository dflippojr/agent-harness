# Installing the agent harness

Run AI agents on your own Windows PC and drive them from a browser or your phone. Use either a local model or your
own hosted-provider CLI subscriptions. Agents work in Docker sandboxes and ask before risky actions.

This iteration supports **Windows 10/11**. The full local-model profile needs an NVIDIA GPU; the hosted-provider
service profile does not.

## Requirements

| | Minimum | Tested |
| --- | --- | --- |
| GPU | Service: none. Full: NVIDIA, 12 GB VRAM, driver 580+ | RTX 4070 Ti Super 16 GB, driver 616.92 |
| RAM | 16 GB (32 GB for the Qwen model) | 32 GB DDR5 |
| Disk | ~20 GB (gpt-oss) or ~30 GB (Qwen) free | NVMe SSD for models |
| Software | [Docker Desktop](https://www.docker.com/products/docker-desktop/) running, [Git](https://git-scm.com/) | Docker 29.7, Git for Windows |

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

Docker Desktop and at least one provider login are the operational minimum. The installer configures Claude, Codex,
and Cursor adapters; an unused provider can remain logged out. Add modules with a PowerShell array, for example
`-EnableModules jobs,backup`. `endpoint`, `images`, and `gpu_guard` automatically opt into `local_model` and restore
the GPU/model requirements. The complete module catalog and security boundary are in
[`service-profile.md`](service-profile.md).

On an existing install, `-Profile Service` writes only `config\profile.yaml`; it preserves `harness.yaml`, local
overrides, paths, and secrets. Run with `-Profile Full` to restore the full profile. With no `-Profile`, upgrades
preserve the existing choice (and use Full for a new install).

## Install

```powershell
git clone https://github.com/dflippojr/agent-harness
cd agent-harness
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

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

## Check it

```powershell
cd agent-harness
& "$env:LOCALAPPDATA\agent-harness\venv\Scripts\python.exe" -m harness.doctor --config-dir "$env:LOCALAPPDATA\agent-harness\config" --instance Main
```

Logs: `%LOCALAPPDATA%\agent-harness\logs` (`daemon.log`, `llama-server.log`, and both supervisors').

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
5. Open the URL on the phone, then Share → Add to Home Screen.

Phone notifications (approvals with Approve/Deny buttons, task finished) use a self-hosted
[ntfy](https://ntfy.sh) server: see `docs/phase2-results.md` for the container and the `notify:` section.

## What else you can turn on

Each is a section in `config\harness.yaml`, documented in the repository's `config/harness.yaml`:

| Feature | Section | Needs |
| --- | --- | --- |
| Projects backed by git repos, with review branches | `config\projects.yaml` | a local repo path |
| Pause for games / Plex transcodes | `gpu_guard` (on by default) | nothing |
| Web search for agents | `web` | SearXNG container (`docs/phase6b-results.md`) |
| OpenAI/Anthropic-compatible endpoint | `endpoint` (on by default) | a key from Settings → Inference endpoint |
| Image generation | `images` | ComfyUI portable + models (`docs/phase6d-results.md`) |
| Claude / Codex / Cursor as session backends | `backends` | `ops\backends\login.ps1 <backend>` (`docs/phase8a-design.md`) |
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

## Security model, briefly

- One owner per install. The web app and APIs trust localhost; other devices need a tailnet login plus, for the
  inference endpoint and app API, a key or token. Optional `guests:` entries grant time-boxed read-only Control
  Center access to a named tailnet login without owner powers.
- Agents are untrusted: shell commands run in a Docker container with only the workspace mounted and no network
  unless you approve it. Pushes, deletes outside scratch paths, and network commands ask first.
- Web fetches refuse private, tailnet and metadata addresses. App-provided context and web pages are marked as
  information, not instructions.
