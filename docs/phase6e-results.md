# Phase 6e: distributable daemon

Built 2026-09-15. Decisions (user): a documented third-party app API plus an installer and setup guide; Windows +
NVIDIA only for this iteration.

## App API

`harness/apps.py`, documented in `docs/app-api.md`; Python SDK `sdk/harness_client.py`; example
`sdk/examples/shopping_list_app.py`.

- **Tokens:** the endpoint's key table gained `kind` (device / app) and `scopes` (`sessions`, `sessions:all`,
  `approvals`, `images`, `inference`). Existing device keys keep `inference`. App tokens start with `ha-`. There's a
  Settings → Apps card to create (pick scopes) and revoke. The inference endpoint now requires the `inference` scope.
- **Sessions** are tagged with the creating app. Apps see only their own, unless they hold `sessions:all`.
- **Context:** blocks at creation go into the system prompt, framed as app-provided information; later blocks
  (`POST .../context`) arrive like messages, as `app_context` events.
- **App tools:** up to 16 per session, stored with it. Names can't collide with built-ins; arguments are
  schema-validated like built-in tools. A call publishes `app_tool_call`, is recorded in `app_tool_calls` (so it
  survives daemon restarts, and apps can recover with `GET .../tool_calls`), and waits for
  `POST .../tool_calls/{call_id}`. Answers within 3 s keep the GPU slot and prompt cache; slower ones release it, and
  the session shows `waiting_app`. Unanswered calls expire after the tool's timeout.
- **Approvals** stay the user's unless the app holds `approvals`, and then only in its own sessions.
- **Images** via `POST /api/v1/images`.
- **Versioning** by path, with a changelog table in `docs/app-api.md`. The shape follows Hermes Agent's `/v1/runs`
  (6a study).

## Installer

`install/install.ps1`, `install/uninstall.ps1`, generic supervisors `install/run-server.ps1` and
`install/run-daemon.ps1`, config generator `harness/setup_config.py`, diagnostics `harness/doctor.py`, and the guide
`docs/INSTALL.md`.

- **No admin needed:** uv (pinned 0.12.14) → Python 3.12 venv; llama.cpp b10950 CUDA 13.3 plus cudart zips; the
  model chosen by VRAM/RAM (Qwen3.6-35B-A3B from `unsloth/Qwen3.6-35B-A3B-GGUF`, or gpt-oss-20b from
  `ggml-org/gpt-oss-20b-GGUF`, both Apache 2.0 and ungated); sandbox image; config; per-user logon tasks
  `AgentHarness-<Instance>-LlamaServer/-Daemon`; then doctor.
- **Resumable downloads** (`curl.exe -C -`); re-running repairs and keeps the config; `-DryRun`; `-ExistingServer`,
  `-ModelPath`, `-LlamaDir` to reuse what's already there; `-Instance` and ports so installs can coexist.
- **Checked prerequisites** (not installed, since that needs admin): NVIDIA driver 580+, Docker Desktop running, Git,
  disk space.
- **Doctor:** config, GPU/driver, data dir, Docker + sandbox image, model server (context size, asleep/paused),
  daemon, GPU guard leftover flag, backups, autostart tasks, optional SearXNG/ComfyUI, tailscale serve. Exits 1 on
  any failure.
- The generated config is deliberately minimal: endpoint, GPU guard and backups on; web, images, memory library,
  notifications and homelab off, with pointers in the guide.
- The tower's own ops scripts and config are unchanged.

## Verification

Tests: 87 pass (1 skipped). `tests/test_phase6.py` adds:

- the app session flow (context in the prompt, app tool round trip with `waiting_app` and a free GPU slot, duplicate
  answers refused, SSE events, context mid-session);
- scopes and isolation (device key can't create sessions, apps can't see each other's or the user's sessions,
  `sessions:all` reads only, approvals need the scope, bad tokens, app tokens refused by the inference endpoint);
- the config generator.

Live on the tower (2026-09-15):

| Check | Result |
| --- | --- |
| SDK example against the main daemon (session `f1c1fea1b6`) | 19 s: Qwen called `list_items`, four `add_item`s and `list_items` again in the script's process, skipped everything in the injected pantry context, and gave a dinner plan |
| `install.ps1 -DryRun` | full plan printed; every download URL answered 200 (llama.cpp 150 MB + cudart 391 MB, uv 18 MB, Qwen 22.4 GB, gpt-oss 12.1 GB) |
| Real install of a test instance (`-InstallDir D:\Agents\install-test -Instance Test -Port 8200 -ExistingServer http://127.0.0.1:8090`) | uv downloaded, Python 3.12 venv + requirements, sandbox image, config, logon task `AgentHarness-Test-Daemon` started, daemon up in 4 s; doctor 0 failed |
| SDK example against the test instance (session `e4c2deb851`) | done in 98 s (including waking the model) |
| `uninstall.ps1 -RemoveFiles` | task removed, test daemon stopped, folder deleted; the main daemon kept running |

**Not tested:** a clean machine; the llama.cpp/model download and `run-server.ps1` running a model server (the
tower's GPU is taken by its own server; the script parses, and the download URLs resolve); machines with 12 GB GPUs;
the gpt-oss preset end to end through the installer (the model itself was benchmarked in Phase 0).
