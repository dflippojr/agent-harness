# Configuration registry

The daemon, `/api/v1`, `/api/admin/v1`, and Agent Harness Web share one typed, allowlisted
registry of operational settings. Unlisted configuration stays in `harness.yaml` /
`harness.local.yaml` / `profile.yaml` and is not writable through these APIs.

This file is the contract for the v1 registry: keys, persistence, recovery, API errors, and
how to add a key safely. It never documents machine-specific paths or secrets.

## Precedence

1. Built-in dataclass defaults.
2. `config/harness.yaml` (checked-in / installer base).
3. `config/harness.local.yaml` (untracked machine overlay; one-level section merge, same as today).
4. `config/profile.yaml` only for `profile`, `modules`, and the existing field-specific model/backend
   starter rules. It is **not** a generic highest-precedence overlay.
5. `data_dir/managed-config.json` — registered **admin** keys only, applied after YAML loading.

Existing installs with no managed overlay behave exactly as before. YAML files are never rewritten
by the registry.

App settings are per `ha-` app id in SQLite (`app_settings`). They never alter host configuration
and stop applying when that app token is revoked. They are kept through the revoke's erasure grace, so an undo
restores them, and are deleted with the App's store when the grace ends.

## Envelope

Managed files are a flat JSON object:

```json
{"schema_version": 1, "revision": 3, "confirmed": true, "values": {"sessions.max_turns": 40}}
```

- `managed-config.json` — active generation (effective live keys; promoted restart keys after a
  confirmed start).
- `managed-config.pending.json` — restart-required candidate.
- `managed-config.lkg.json` — last confirmed good managed generation.
- `managed-config.quarantine.json` — a failed candidate, with a reason.
- `managed-config.json.quarantine.<UTC>` / `managed-config.lkg.json.quarantine.<UTC>` — timestamped
  copies of overlay files that could not be applied (active and LKG both invalid). YAML defaults
  are in effect until the owner inspects and deletes or repairs them.

Writes use a process lock, a temp file, flush/fsync where supported, atomic replace, and `0600`
permissions where the OS allows. The files contain no secrets and no nested YAML.

`GET` responses include a monotonic `revision` and `ETag`. `PATCH` / validate / rollback send the
expected base `revision`. A stale writer receives `409` `revision_conflict` instead of losing
updates. A batch is all-or-nothing.

`null` in a change map is reset-to-inherited: the key is removed from the overlay rather than
copying a default.

## Live, pending, restart, recovery

- **live** keys write the new generation and run each apply hook. If a hook fails, already-applied
  hooks are undone, the previous file and in-memory values are restored, and the failure is audited.
- **daemon_restart** keys are saved as pending. They are visible separately from effective values and
  do not change the running process until an owner-confirmed restart.
- `restart_required` is true when a pending file exists **or** when the confirmed/active overlay's
  `daemon_restart` values differ from what this process has applied (for example after rolling back
  `web.enabled`). GET `/api/admin/v1/config` and the rollback response use that flag so the Web UI
  can offer Restart. It is not derived only from a pending file.
- Supervisors (Windows scheduled-task wrapper, systemd-user `run-daemon.sh`, launchd `run-daemon.sh`)
  set `HARNESS_SUPERVISED=1`. `POST /api/admin/v1/config/restart` then returns `202` and asks this
  process to exit; the supervisor starts it again. The API never shells out to Task Scheduler,
  `systemctl`, or `launchctl` and never elevates.
- An unsupervised `python -m harness` may save pending values, but restart returns `409`
  `restart_not_supervised` with manual instructions and does not kill the process.
- Before promoting pending values, the current confirmed generation is copied to LKG and the
  candidate is marked unconfirmed. After configuration load, database migration, manager startup,
  and `/health`, the revision is confirmed and pending state is cleared.
- If that candidate fails validation/startup, or the next supervisor start still sees it unconfirmed,
  it is quarantined and LKG is restored. Invalid base/local/profile YAML is never masked by this
  fallback.
- **Boot never fails because of the managed overlay.** If applying confirmed active raises, LKG is
  tried next. If LKG also fails (for example both pin `backends.local.model` to a name later removed
  from `harness.yaml`), both files are renamed aside with a timestamp
  (`managed-config.json.quarantine.<UTC>`, `managed-config.lkg.json.quarantine.<UTC>`), the process
  starts on YAML defaults, and GET `/api/admin/v1/config` surfaces `recovery: overlay_quarantined`
  plus a `warning`. The quarantined files are kept for the owner. Invalid YAML still fails boot.
- Owner rollback restores the single previous confirmed managed generation through the same
  validation path (live or pending according to the keys). Rollback of a `daemon_restart` key writes
  LKG onto confirmed active and does not apply that key in-memory; `restart_required` stays true
  until an owner-confirmed restart loads the restored overlay.

### Overlay state machine

Boot applies **active** only. Pending is never loaded until an owner-confirmed restart
copies it onto active. Every generation change is a single `ManagedStore.commit`
(write temp → fsync → replace; active is the commit point).

| Disk state | Meaning | Next boot |
| --- | --- | --- |
| no active | YAML only | YAML; nothing to confirm |
| confirmed active | live keys + last *promoted* restart keys | apply active; do not restore LKG |
| pending file | candidate after a `daemon_restart` PATCH | ignore pending; keep applying confirmed active |
| unconfirmed active, boot-tried clear | owner confirmed restart; first start | try candidate once |
| unconfirmed active, boot-tried set | previous start died before `/health` confirm | quarantine candidate; restore LKG |
| quarantine + restored LKG | failed generation | apply confirmed LKG |
| active and LKG both unusable | YAML dropped a pinned overlay value | timestamp-quarantine both; YAML defaults; `overlay_quarantined` |

A PATCH of a restart key (for example `web.enabled`) must not drop that key from
confirmed active. The new value lives in pending until restart. If the process dies
before `POST /config/restart`, the next start still applies the last confirmed
overlay (YAML cannot turn the feature back on behind a confirmed `false`).

## Admin keys (v1)

Writable live (new sessions pick up new defaults; an active run's frozen model/backend/effort and
budget are not increased):

| Key | Bounds |
| --- | --- |
| `sessions.max_turns` | 1–500 |
| `sessions.max_completion_tokens` | 1_000–2_000_000 |
| `compaction.elide_at` | 0.10–0.90; must be `< reset_at` and `< summarize_at` |
| `compaction.reset_at` | 0.15–0.95; must be `> elide_at` and `< summarize_at`. Round reset (explicit `reset_round` or this threshold with valid saved state) runs after masking and skips elide/summary. |
| `compaction.summarize_at` | 0.15–0.95 |
| `compaction.keep_recent` | 0.05–0.50; must be `< summarize_at` |
| `compaction.state_max_chars` | 256–100_000; cap on the serialized `update_state` object |

Round-reset `files_modified` is derived at reset on tower agent sessions: the union of `git diff --name-status` from the run-start HEAD to current HEAD, plus staged, unstaged, and untracked paths (including deleted and renamed). Files already dirty at run start are included only if their content changed. Chat sessions, hosted CLI backends, and non-tower targets (Mac or other remote workspaces) do not use git for that list; they fall back to paths from `write_file` / `edit_file` if a reset happens at all. Reset quality depends on the model writing useful state before a threshold trigger; an automatic reset with empty state is not a dead-end record and falls through to ordinary compaction. Saved state is re-injected as a tagged, size-capped user message and is never executed.
| `cleanup.container_idle_hours` | 0.25–168 |
| `cleanup.workspace_retention_days` | 1–365 |
| `cleanup.workspace_quota_mb` | 50–100_000 |
| `cleanup.min_free_gb` | 1–1000 |
| `cleanup.interval_minutes` | 5–1440 (rescheduled live) |
| `web.page_chars` / `web.max_bytes` / `web.max_document_bytes` / `web.timeout_seconds` / `web.quote_check` | web module installed |
| `endpoint.max_waiting` / `endpoint.agent_fair_seconds` / `endpoint.request_timeout_seconds` | endpoint module |
| `images.start_timeout_seconds` / `images.job_timeout_seconds` | images module |
| `images.edit_enabled` / `images.max_upload_bytes` / `images.max_pixels` | opt-in image_edit module. `max_pixels` is the decoded-pixel cap for uploads and gallery edits; gallery sources over 1664 px on the long side or over `max_pixels` are rejected (uploads downscale to 1664) |
| `gpu_guard.poll_seconds` / `gpu_guard.resume_after_seconds` / `gpu_guard.drain_timeout_seconds` | gpu_guard module |
| `jobs.poll_seconds` | jobs module |
| `backup.at` (`HH:MM`) / `backup.keep_days` | backup module |
| `smart_approvals.enabled` / `smart_approvals.mode` / `smart_approvals.provider` / `smart_approvals.model` / `smart_approvals.timeout_seconds` / `smart_approvals.min_confidence` | hosted reviewer; `secret_ref` stays file-only. `mode` last-writer: this setting and `PUT /smart-approvals` share one SQLite overlay; `off` means no reviewer calls |
| `backends.local.model` | installed local models |
| `backends.<name>.model` / `backends.<name>.effort` | each configured hosted backend |

`compaction.mask_min_chars` is file-only (`config/harness.yaml`, clamped 1–10_000_000) and is not a live admin key. See [`compaction.md`](compaction.md).

Restart-required feature switches (installed module + valid file config; enabling fails if required
URLs, token files, models, or platform support are missing). These change effective daemon features,
not `modules.*` installation. `capabilities.modules.<name>`, `/health`, `/api/v1` `features`, and
the Manager's tool construction are derived from `installed AND <section>.enabled` in one place
(`module_effective`); overlay setters never write `cfg.modules`.

`web.enabled`, `search.enabled`, `jobs.enabled`, `endpoint.enabled`, `images.enabled`,
`gpu_guard.enabled`, `notifications.enabled`, `backup.enabled`.

An add-on module ([`modules.md`](modules.md)) brings its own keys (images: `images.enabled`,
`images.edit_enabled`, `images.*` limits, `modules.images`). They exist only while the module is present; a
saved overlay value for an absent module's key is kept but not applied, so uninstalling one never quarantines
the overlay.

Installer/file-only metadata (value omitted): `listen.host`, `listen.port`, `paths.data_dir`, `paths.repos_dir`,
`backup.dir`, `notify.server`, `notify.topic`, `notify.token_file`, `smart_approvals.secret_ref`,
`smart_approvals.proxy`, `install.profile`, `modules.*`, `compaction.mask_min_chars`.

## App keys (v1)

Require a live app token (`ha-`) and the related scope. An app value may only narrow authority.

| Key | Notes |
| --- | --- |
| `app.default_backend` / `app.default_model` / `app.default_effort` | used only when a session request omits them |
| `app.sessions.max_turns` / `app.sessions.max_completion_tokens` | capped by owner limits (`capped_by`) |
| `app.capabilities` | subset of `web`, `search`, `memory_library`, `remote_control`, `homelab` (and `images` while that module is present) already granted, installed, and allowed. Unset means every capability the token's scopes grant; `memory_library` and `homelab` need their own scope, which only the owner sets |
| `app.notify.completion` | `inherit` or `never` |

Owner, device, runner, guest, and anonymous credentials cannot impersonate app configuration.

## Registered keys (generated)

Every key of the registry with all modules present, read from `harness/settings_keys.py` by `scripts/docs/build.py` (do not edit between the markers). Defaults for installer-only, path-like, secret and per-backend keys are not shown. The tables above explain bounds and behaviour.

<!-- generated:begin config-registry-keys -->
| Key | Type | Default | Scope | Description |
| --- | --- | --- | --- | --- |
| `app.capabilities` | string_list | — | app | Subset of capabilities already granted by the token, installed by the daemon, and allowed by policy. |
| `app.default_backend` | string | `""` | app | Used only when a session request omits backend. Cannot select an unassigned provider. |
| `app.default_effort` | enum | `""` | app | Used only when a hosted-backend session request omits effort. |
| `app.default_model` | string | `""` | app | Used only when a session request omits model. |
| `app.notify.completion` | enum | `"inherit"` | app | inherit uses the owner channel; never silences this app's session-completion notifications. |
| `app.sessions.max_completion_tokens` | int | — | app | Per-session completion-token cap, never higher than the owner limit. |
| `app.sessions.max_turns` | int | — | app | Per-session turn cap, never higher than the owner limit. |
| `backends.claude.effort` | enum | — | admin | Default effort for new claude sessions. Existing sessions keep the effort they started with. |
| `backends.claude.model` | string | — | admin | Default model for new claude sessions. Existing sessions keep the model they started with. |
| `backends.codex.effort` | enum | — | admin | Default effort for new codex sessions. Existing sessions keep the effort they started with. |
| `backends.codex.model` | string | — | admin | Default model for new codex sessions. Existing sessions keep the model they started with. |
| `backends.local.model` | enum | — | admin | Default local model for new sessions. Existing sessions keep the model they started with. |
| `backup.at` | string | `"03:30"` | admin | Local time (HH:MM) for the nightly backup. |
| `backup.dir` | string | — | admin | Where nightly backups are written. |
| `backup.enabled` | bool | `false` | admin | Runtime enable for the nightly backup. Does not change the backup directory. |
| `backup.keep_days` | int | `14` | admin | Delete dated backup folders older than this. |
| `backup.member_key_dir` | string | — | admin | Separate key copies; keep apart from database backups off-site. Unset uses backup.dir/member-keys. |
| `cleanup.container_idle_hours` | float | `24` | admin | Remove a finished session's stopped container after this many hours. |
| `cleanup.interval_minutes` | int | `60` | admin | How often idle containers and old workspaces are swept. Applied live by rescheduling the cleanup task. |
| `cleanup.min_free_gb` | float | `20` | admin | Refuse new sessions when the data drive has less free space than this. |
| `cleanup.workspace_quota_mb` | int | `5000` | admin | Per-session workspace limit unless a project overrides it. |
| `cleanup.workspace_retention_days` | float | `14` | admin | Delete a finished session's workspace after this many days. |
| `compaction.elide_at` | float | `0.55` | admin | Fraction of context at which old tool outputs are shortened. |
| `compaction.keep_recent` | float | `0.2` | admin | Fraction of context kept verbatim after a summary. Must be less than summarize_at. |
| `compaction.reset_at` | float | `0.6` | admin | Fraction of context at which a round reset fires when valid state is saved. Must be greater than elide_at and less than summarize_at. |
| `compaction.state_max_chars` | int | `8000` | admin | Maximum characters of the serialized update_state object. Saved state is re-injected on a round reset. |
| `compaction.summarize_at` | float | `0.65` | admin | Fraction of context at which older turns are summarized. Must be greater than elide_at. |
| `endpoint.agent_fair_seconds` | float | `90` | admin | After an agent turn waits this long, new endpoint requests queue behind it. |
| `endpoint.enabled` | bool | `false` | admin | Runtime enable for the OpenAI/Anthropic-compatible endpoint. |
| `endpoint.max_waiting` | int | `4` | admin | Inference requests waiting for the GPU before new ones get 429. |
| `endpoint.request_timeout_seconds` | float | `1800` | admin | Give up on a hung inference request after this long. |
| `gpu_guard.drain_timeout_seconds` | float | `300` | admin | Longest wait for the current model turn before the server is stopped. |
| `gpu_guard.enabled` | bool | `false` | admin | Runtime enable for pausing the model while a game or Plex transcode needs the GPU. |
| `gpu_guard.poll_seconds` | float | `10` | admin | How often the GPU guard looks for games or Plex transcodes. |
| `gpu_guard.resume_after_seconds` | float | `180` | admin | The GPU must stay clear this long before the model is reloaded. |
| `images.edit_enabled` | bool | `false` | admin | Runtime enable for the installed Qwen-Image-Edit component. Does not download model weights. |
| `images.enabled` | bool | `false` | admin | Runtime enable for local image generation. |
| `images.job_timeout_seconds` | float | `1200` | admin | How long a single image job may run. |
| `images.max_pixels` | int | `20000000` | admin | Reject gallery edits and decoded uploads/masks above this pixel count (long side is also capped at 1664). |
| `images.max_upload_bytes` | int | `20971520` | admin | Maximum source or mask upload size for owner-only masked editing. |
| `images.start_timeout_seconds` | float | `180` | admin | How long to wait for ComfyUI to become ready. |
| `install.profile` | string | — | admin | full or service. Chosen by the installer. |
| `jobs.enabled` | bool | `false` | admin | Runtime enable for scheduled jobs. Does not install the jobs module. |
| `jobs.poll_seconds` | float | `30` | admin | How often scheduled jobs are checked. |
| `listen.host` | string | — | admin | Bind address for the daemon HTTP server. |
| `listen.port` | string | — | admin | TCP port for the daemon HTTP server. |
| `memory_library.enabled` | bool | `false` | admin | Runtime enable for personal memory tools. Does not install the memory library module. |
| `modules.backup` | string | — | admin | backup installation/profile selection. Change this with the installer, not this registry. |
| `modules.endpoint` | string | — | admin | endpoint installation/profile selection. Change this with the installer, not this registry. |
| `modules.gpu_guard` | string | — | admin | gpu_guard installation/profile selection. Change this with the installer, not this registry. |
| `modules.homelab` | string | — | admin | homelab installation/profile selection. Change this with the installer, not this registry. |
| `modules.image_edit` | string | — | admin | image_edit installation/profile selection. Change this with the installer, not this registry. |
| `modules.images` | string | — | admin | images installation/profile selection. Change this with the installer, not this registry. |
| `modules.jobs` | string | — | admin | jobs installation/profile selection. Change this with the installer, not this registry. |
| `modules.local_model` | string | — | admin | local_model installation/profile selection. Change this with the installer, not this registry. |
| `modules.mcp_client` | string | — | admin | mcp_client installation/profile selection. Change this with the installer, not this registry. |
| `modules.memory_library` | string | — | admin | memory_library installation/profile selection. Change this with the installer, not this registry. |
| `modules.notifications` | string | — | admin | notifications installation/profile selection. Change this with the installer, not this registry. |
| `modules.remote_control` | string | — | admin | remote_control installation/profile selection. Change this with the installer, not this registry. |
| `modules.runners` | string | — | admin | runners installation/profile selection. Change this with the installer, not this registry. |
| `modules.search` | string | — | admin | search installation/profile selection. Change this with the installer, not this registry. |
| `modules.skills` | string | — | admin | skills installation/profile selection. Change this with the installer, not this registry. |
| `modules.web` | string | — | admin | web installation/profile selection. Change this with the installer, not this registry. |
| `notifications.enabled` | bool | `false` | admin | Runtime enable for ntfy notifications. Does not configure a server or topic. |
| `notify.server` | string | — | admin | ntfy server URL. |
| `notify.token_file` | string | — | admin | File holding the ntfy write token. |
| `notify.topic` | string | — | admin | ntfy topic name. |
| `paths.data_dir` | string | — | admin | SQLite database, workspaces, and transcripts. |
| `paths.repos_dir` | string | — | admin | Host-side clones for local:<name> git_clone. |
| `remote_control.discovery.enabled` | bool | `false` | admin | Windows owner-only, default-off metadata discovery. No file contents, trust or launch. Limits: 20,000 directories; 500 candidates; 30 seconds; 50 errors; one active scan; results expire after 15 minutes. Hidden/system entries, all reparse points (including OneDrive), credentials, caches and build folders are excluded. |
| `remote_control.discovery.max_depth` | int | `3` | admin | Windows owner-only, default-off metadata discovery. No file contents, trust or launch. Limits: 20,000 directories; 500 candidates; 30 seconds; 50 errors; one active scan; results expire after 15 minutes. Hidden/system entries, all reparse points (including OneDrive), credentials, caches and build folders are excluded. |
| `remote_control.discovery.roots` | discovery_root_list | `[]` | admin | Windows owner-only, default-off metadata discovery. No file contents, trust or launch. Limits: 20,000 directories; 500 candidates; 30 seconds; 50 errors; one active scan; results expire after 15 minutes. Hidden/system entries, all reparse points (including OneDrive), credentials, caches and build folders are excluded. |
| `remote_control.enabled` | bool | `false` | admin | Runtime enable for Remote Control. Does not install the module. |
| `search.enabled` | bool | `false` | admin | Runtime enable for session search. Does not install the search module. |
| `sessions.app_max_queued` | int | `4` | admin | How many sessions an App may have queued or parked before new ones are refused (429), unless the owner set its own cap. |
| `sessions.app_max_running` | int | `2` | admin | How many sessions an App may have running or parked at once, unless the owner set its own cap. |
| `sessions.approval_timeout_seconds` | float | `86400` | admin | Deny a pending approval nobody decided after this long and end its run. 0 never expires one. |
| `sessions.max_completion_tokens` | int | — | admin | Per-run completion-token cap for new sessions. Changing this does not raise an active run's budget. |
| `sessions.max_run_seconds` | float | `3600` | admin | End a member's or an App's run after this long running (approval, queue and reply waits do not count). 0 is no limit. The owner's own runs have none. |
| `sessions.max_turns` | int | `80` | admin | Per-run turn cap for new sessions. Changing this does not raise an active run's budget. |
| `skills.enabled` | bool | `false` | admin | Runtime enable for owner-approved instruction skills. Does not install the skills module. |
| `smart_approvals.enabled` | bool | `false` | admin | Runtime enable for the hosted smart-approval reviewer. Does not configure a secret_ref. |
| `smart_approvals.min_confidence` | float | `0.85` | admin | Hosted reviewer must meet this confidence before auto mode may approve. |
| `smart_approvals.mode` | enum | `"shadow"` | admin | off, shadow, or auto. Last writer among this setting and PUT /smart-approvals wins; off means no reviewer calls. |
| `smart_approvals.model` | string | `"gpt-4.1-mini"` | admin | Hosted reviewer model id. Existing in-flight reviews keep the model they started with. |
| `smart_approvals.provider` | enum | `"openai"` | admin | Hosted reviewer provider. openai or anthropic. |
| `smart_approvals.proxy` | string | — | admin | Optional explicit proxy for the hosted reviewer. Managed in local configuration. |
| `smart_approvals.secret_ref` | string | — | admin | Opaque name of the hosted reviewer key file. Managed in local configuration. |
| `smart_approvals.timeout_seconds` | float | `8` | admin | Give up on a hung reviewer request after this long. |
| `web.enabled` | bool | `false` | admin | Runtime enable for web_search / web_fetch. Does not install the web module. |
| `web.max_bytes` | int | `5242880` | admin | Refuse larger ordinary page downloads. |
| `web.max_document_bytes` | int | `26214400` | admin | Refuse larger PDF or Word downloads. |
| `web.page_chars` | int | `15000` | admin | Characters returned per web_fetch page. |
| `web.quote_check` | bool | `true` | admin | Require quoted passages in final answers to appear in something the agent read. |
| `web.timeout_seconds` | float | `20` | admin | Network timeout for search and fetch. |
<!-- generated:end config-registry-keys -->

## APIs

App: `GET/PATCH /api/v1/config`, `GET /api/v1/config/schema`.

Owner: `GET /api/admin/v1/config`, `GET /api/admin/v1/config/schema`,
`POST /api/admin/v1/config/validate`, `PATCH /api/admin/v1/config`,
`POST /api/admin/v1/config/rollback`, `POST /api/admin/v1/config/restart`.

Restart and rollback require `"confirm": true`. Feature enables require a confirmation in the Web UI.

Stable error codes: `unknown_key`, `invalid_value`, `validation_error`, `revision_conflict`,
`dependency`, `installer_only`, `forbidden`, `confirmation_required`, `restart_not_supervised`,
`nothing_to_rollback`, `apply_failed`. Per-key details never include hidden values.

## Audit

`data_dir/config-audit.jsonl` records actor kind/id, timestamp, revision, action, keys, and
before/after values only for explicitly non-sensitive settings. Hidden and path-like keys are
redacted even if a bug marks them writable. Credentials, secret/file contents, authorization
headers, machine-specific paths, and raw request bodies are never logged. The file has no rotation. Nightly backups carry a
complete-line prefix of it and restore archives it (see `docs/INSTALL.md`, "Backups and restore").

## Adding a key

1. Add a `SettingSpec` in `harness/settings_keys.py` with an explicit getter and setter, bounds,
   scope, apply mode, sensitivity, and module/capability dependencies. Do not generate setters with
   reflection, dotted traversal, dataclass deserialization, or YAML merge.
2. If it is restart-required, omit a live hook. If it can apply live, add apply/undo hooks that are
   transactional with the rest of the batch.
3. Cover it in `tests/test_config_registry.py` (getter/setter, bounds, unknown-key rejection).
4. Document it in the table above. Never add secrets, host paths, `modules.*`, sandbox/network, or
   identity fields to the writable registry.
# Remote Control discovery

The owner registry includes three live Windows-only discovery settings using a
dedicated root-list type. Discovery is default-off and exposes no app or agent
capability. See [owner folder discovery](remote-control-discovery.md) for limits,
validation, promotion and separate Claude trust.
