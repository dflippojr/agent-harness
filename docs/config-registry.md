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
and are deleted when that app token is revoked.

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
| `compaction.elide_at` | 0.10–0.90; must be `< summarize_at` |
| `compaction.summarize_at` | 0.15–0.95 |
| `compaction.keep_recent` | 0.05–0.50; must be `< summarize_at` |
| `cleanup.container_idle_hours` | 0.25–168 |
| `cleanup.workspace_retention_days` | 1–365 |
| `cleanup.workspace_quota_mb` | 50–100_000 |
| `cleanup.min_free_gb` | 1–1000 |
| `cleanup.interval_minutes` | 5–1440 (rescheduled live) |
| `web.page_chars` / `web.max_bytes` / `web.max_document_bytes` / `web.timeout_seconds` / `web.quote_check` | web module installed |
| `endpoint.max_waiting` / `endpoint.agent_fair_seconds` / `endpoint.request_timeout_seconds` | endpoint module |
| `images.start_timeout_seconds` / `images.job_timeout_seconds` | images module |
| `images.edit_enabled` / `images.max_upload_bytes` / `images.max_pixels` | opt-in image_edit module |
| `gpu_guard.poll_seconds` / `gpu_guard.resume_after_seconds` / `gpu_guard.drain_timeout_seconds` | gpu_guard module |
| `jobs.poll_seconds` | jobs module |
| `backup.at` (`HH:MM`) / `backup.keep_days` | backup module |
| `smart_approvals.enabled` / `smart_approvals.mode` / `smart_approvals.provider` / `smart_approvals.model` / `smart_approvals.timeout_seconds` / `smart_approvals.min_confidence` | hosted reviewer; `secret_ref` stays file-only. `mode` last-writer: this setting and `PUT /smart-approvals` share one SQLite overlay; `off` means no reviewer calls |
| `backends.local.model` | installed local models |
| `backends.<name>.model` / `backends.<name>.effort` | each configured hosted backend |

Restart-required feature switches (installed module + valid file config; enabling fails if required
URLs, token files, models, or platform support are missing). These change effective daemon features,
not `modules.*` installation. `capabilities.modules.<name>`, `/health`, `/api/v1` `features`, and
the Manager's tool construction are derived from `installed AND <section>.enabled` in one place
(`module_effective`); overlay setters never write `cfg.modules`.

`web.enabled`, `search.enabled`, `jobs.enabled`, `endpoint.enabled`, `images.enabled`,
`gpu_guard.enabled`, `notifications.enabled`, `backup.enabled`.

Installer/file-only metadata (value omitted): `listen.host`, `listen.port`, `paths.data_dir`, `paths.repos_dir`,
`backup.dir`, `notify.server`, `notify.topic`, `notify.token_file`, `smart_approvals.secret_ref`,
`smart_approvals.proxy`, `install.profile`, `modules.*`.

## App keys (v1)

Require a live app token (`ha-`) and the related scope. An app value may only narrow authority.

| Key | Notes |
| --- | --- |
| `app.default_backend` / `app.default_model` / `app.default_effort` | used only when a session request omits them |
| `app.sessions.max_turns` / `app.sessions.max_completion_tokens` | capped by owner limits (`capped_by`) |
| `app.capabilities` | subset of `web`, `images`, `search`, `memory_library`, `remote_control`, `homelab` already granted, installed, and allowed |
| `app.notify.completion` | `inherit` or `never` |

Owner, device, runner, guest, and anonymous credentials cannot impersonate app configuration.

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
headers, machine-specific paths, and raw request bodies are never logged.

## Adding a key

1. Add a `SettingSpec` in `harness/settings_keys.py` with an explicit getter and setter, bounds,
   scope, apply mode, sensitivity, and module/capability dependencies. Do not generate setters with
   reflection, dotted traversal, dataclass deserialization, or YAML merge.
2. If it is restart-required, omit a live hook. If it can apply live, add apply/undo hooks that are
   transactional with the rest of the batch.
3. Cover it in `tests/test_config_registry.py` (getter/setter, bounds, unknown-key rejection).
4. Document it in the table above. Never add secrets, host paths, `modules.*`, sandbox/network, or
   identity fields to the writable registry.
