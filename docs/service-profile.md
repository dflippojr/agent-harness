# Hosted-provider service profile

The `service` profile is a small, hosted-provider-first daemon. It keeps the same sessions, provider adapters,
approval flow, event stream, scoped tokens, persistent storage, Control Center, and versioned APIs as the full
install, without requiring a GPU or local model.

## Install and discover

```powershell
powershell -ExecutionPolicy Bypass -File install\install.ps1 -Profile Service
ops\backends\login.ps1 codex # or claude / cursor; credentials stay in a Docker volume
```

Minimum runtime requirements are Windows 10/11, Git, Docker Desktop, and a login for at least one configured
provider. `GET /health`, `GET /api/v1`, and authenticated `GET /api/admin/v1` return the same `capabilities` object:

```json
{
  "profile": "service",
  "required": {"sessions": true, "provider_adapters": true, "approvals": true, "events": true,
               "scoped_tokens": true, "storage": true, "capability_discovery": true},
  "modules": {"local_model": false, "jobs": false, "web": false},
  "hosted_backends": ["claude", "codex", "cursor"]
}
```

The actual response includes every module. Clients should use capability discovery instead of assuming that model,
GPU, jobs, image, memory, runner, or other optional routes are usable.

## Optional modules

All optional modules default off in the service profile: `local_model`, `homelab`, `memory_library`, `images`,
`jobs`, `gpu_guard`, `runners`, `remote_control`, `web`, `search`, `endpoint`, `notifications`, and `backup`.
Opt in during install with `-EnableModules jobs,backup`. The `endpoint`, `images`, and `gpu_guard` modules depend on
`local_model`; the installer enables it automatically and applies the full GPU/model checks.

The profile overlay is `config\profile.yaml`. `harness.yaml` and `harness.local.yaml` retain detailed settings. This
makes an upgrade reversible:

```powershell
install\install.ps1 -Profile Service # preserve the full config, run hosted-only
install\install.ps1 -Profile Full    # use the preserved full configuration again
```

An installer run without `-Profile` preserves an existing profile. It defaults to Full only for a new install.

## Threat boundary

- The daemon binds to localhost; Tailscale Serve is the supported remote entry point. Owner and app APIs retain
  their existing identity, origin, scope, and token checks.
- Provider CLIs are unmodified and run in workspace-mounted Docker containers. Each provider has a separate auth
  volume and internal network. The daemon invokes those volumes but does not read credentials from them.
- Each provider network reaches the internet only through its own allowlist proxy. General sandbox network access
  remains approval-gated.
- Capability responses list enabled facilities and provider names only; they never include tokens, API-key values,
  secret paths, or provider credentials.
- Admin filesystem browsing is not a service-profile capability and remains disabled for app clients. The optional
  future work tracked in issue #67 does not change this profile's default boundary.
