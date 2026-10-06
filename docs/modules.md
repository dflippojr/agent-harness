# Optional modules

Issue #334 splits Agent Harness Server into a core and optional add-on modules. This page is the module
interface ([`harness/modules.py`](../harness/modules.py)) and how to write a module. Images
([`harness_modules/images/`](../harness_modules/images/)) is the first module behind it notifications
([`harness_modules/notifications/`](../harness_modules/notifications/)) the second, session search
([`harness_modules/search/`](../harness_modules/search/)) the third and the nightly backup
([`harness_modules/backup/`](../harness_modules/backup/)) the fourth and instruction skills
([`harness_modules/skills/`](../harness_modules/skills/)) the fifth, followed by homelab and the memory library
([`harness_modules/memory_library/`](../harness_modules/memory_library/)); the other optional features move
one module per PR in stage (c).

## Rules

- **One registration point.** A module is a package whose `MODULE` attribute is a `harness.modules.Module`. Everything
  the module adds to the daemon is a field of that object. The core asks `harness/modules.py` for contributions and
  never imports a module package (`tests/test_modules.py` scans `harness/` for it).
- **Modules import the core only through `harness.modules`.** Its `_PUBLIC` table re-exports the core objects a
  module may use (`HarnessError`, `RouteTable`, `require_owner`, `app_auth`, `SettingSpec`, `ToolError`,
  `ServerControl`, …). A module never imports another module. Both are enforced by a test.
- **Absent means absent.** A module is *present* when its package is discovered and its first service-profile
  switch is installed (`cfg.installed.<switch>`, [#26](service-profile.md)). An absent module (service profile, or
  the package isn't there) adds no routes (they answer 404, or 405 from the static site for other methods), no
  tools, no settings keys, no capability entries, no App scope, and the daemon starts as usual.
- **Present but switched off is not absent.** `images.enabled: false` keeps the module's settings keys (so the owner
  can turn it back on) and its routes, which answer "image generation is disabled" as before.

## Discovery

A configured list: `module_packages` in `config/harness.yaml` (or `harness.local.yaml`), in load order:

```yaml
module_packages: [harness_modules.images]   # [] runs the core alone
```

Without the key the list is every package under the `harness_modules` namespace, so installing a module is putting
its package there and uninstalling it is removing it. A listed package that fails to import is logged and skipped:
a broken add-on never stops the core.

Why a list and not Python entry points: the Server runs from a checkout (`python -m harness`, deployed by
fast-forwarding the live checkout), not from an installed distribution, so there is no package metadata for entry
points to read. A list is explicit, ordered and testable (tests pass their own), and the namespace default needs no
configuration for an ordinary install.

## Layout

```
harness/                 the core (moves to its own repository in stage f)
  modules.py             the interface, discovery and ModuleHost
harness_modules/         a PEP 420 namespace package: no __init__.py, so separate distributions can each add one
  images/
    __init__.py          MODULE = Module(...): the registration point; light, the CLI imports it
    runtime.py           ImagesRuntime: lifecycle, GPU hand-over, tools, metrics
    routes.py            owner (/images, image-archive retention) and App API (/api/v1/images) routes
    settings.py          the images.* registry keys
    doctor.py            python -m harness.doctor checks
    service.py edit.py archive.py models.py upscale.py flux_fast.json   (formerly harness/images*.py etc.)
  notifications/         ntfy phone notifications (formerly harness/notify.py)
    __init__.py runtime.py routes.py settings.py service.py
  search/                session search (formerly harness/search.py): /search, /api/v1/search, session_search/read
    __init__.py runtime.py routes.py settings.py service.py
  backup/                nightly backup (formerly Maintenance's backup section) and verify/restore (#374)
    __init__.py runtime.py routes.py settings.py service.py restore.py
  skills/                owner-approved instructions (formerly harness/skills.py, skill_review.py, skill_validate.py)
    __init__.py runtime.py routes.py settings.py service.py skill_review.py skill_validate.py
  memory_library/        personal memory tools, approved writes and frozen session profiles (formerly harness/memory_library.py)
    __init__.py runtime.py routes.py settings.py service.py
```

Modules sit beside the core, not inside it, so stage (f) can move `harness/` to the new repository unchanged while
`harness_modules/` stays here as add-on packages: nothing in either tree has to be pulled out of the other, and a
module's only link back is `harness.modules`.

## What a module contributes

| `Module` field | What the core does with it |
| --- | --- |
| `name`, `switches` | `switches[0]` decides presence; every switch gets a `capabilities.modules.<switch>` entry and a hidden `modules.<switch>` registry key while present. |
| `runtime_enabled(cfg, switch)` | The runtime switch behind a profile switch (`cfg.images.enabled`). `module_effective` = installed AND this. |
| `runtime(manager, module)` | Builds a `ModuleRuntime` (below) for each Manager. |
| `owner_routes()` | A `RouteTable` installed with the core's daemon routes, behind the same owner/guest/member guard. Handlers call `require_owner` where the route is owner-only. |
| `admin_paths` | Owner routes also served under `/api/admin/v1` (owner credential required) and listed in its `operations`. |
| `app_routes()` | A `RouteTable` of `/api/v1/...` routes. Handlers call `app_auth(request, scope)`. |
| `public_routes()` | Unauthenticated routes (none today). |
| `app_scopes`, `app_capabilities` | App token scopes it adds, and `app.capabilities` values (`{capability: scope}`). |
| `tools` (`ToolGate`), `tool_names` | When the runtime's `toolkit()` is offered to a session: project flag, App capability, members, MCP for hosted sessions, whether it writes into the workspace (`workspace_root` / runner `put_bytes`), which tools mutate files (quota and checkpoints), the telemetry span, the system-prompt section (`prompt`) a session gets with it. `tool_names` are reserved against App tools. |
| `settings()` | `SettingSpec`s with defaults, bounds, `enable_check`s and named getters/setters, merged into the registry. |
| `cli`, `cli_groups` | Rows in `harness.cli` `ADMIN_COMMANDS` format (`harness images …`); the stage (a) parity rows. The Mac client bundle carries a JSON copy (`mac_client.py`). |
| `principal_capabilities(owner, scopes)` | Entries for `/me` and `/api/v1/me` `capabilities`. |
| `doctor(report, cfg)` | Checks for `python -m harness.doctor`. |
| `docs` | Pointers to the module's documentation. |

`ModuleRuntime` hooks, all optional:

| Hook | When |
| --- | --- |
| `__init__` | In `Manager.__init__`, before Maintenance. Set `self.backup` to join the nightly backup (images: the image archive). |
| `init()` | After the managed settings overlay is applied: create services here (`self.service`). |
| `wire_resources(guard, warmer)` | The resource guard is on: take its RAM check and lazy-load preference. |
| `start()` / `stop()` | Daemon start and shutdown. |
| `toolkit()` | The object with `tool_names`, `schemas()` and `call()`; offered per `Module.tools`. |
| `gpu_taken`, `busy()`, `gpu_hold()`, `gpu_resume(ids)` | GPU interaction: the model warmer stays parked while `gpu_taken`; skill review waits while `busy()`; the GPU guard's pause and resume. |
| `features()`, `app_root()` | `/api/v1` `features` entries and extra top-level keys (`image_modes`). |
| `metrics(out, db)` | Prometheus lines on `/metrics`. |

`manager.<module name>` returns the runtime's `service` (None while switched off or absent), for code written before
modules; new core code goes through `manager.modules`.

Session-specific native toolkits (#260): `ToolGate.per_session=True` selects
`runtime.session_toolkit(session)` through `ModuleHost.toolkits(session)`. The native loop awaits
`prepare_session(session)` before its first listing and calls `end_session(sid)` on completion, cancellation or
failure. These hooks default to no-ops. `owns_toolkit(kit)` lets a runtime associate cached per-session toolkits
with its gate for dispatch and tracing. A per-session toolkit is excluded from sessionless listings; set
`mcp=False` to exclude it from hosted sessions. The owner-pinned MCP client uses this interface; see
[`mcp-client.md`](mcp-client.md). Its core configuration names (`Project.mcp_servers`, `ModulesConfig.mcp_client`
and validation in `harness/mcp_config.py`) remain in core so file validation works with the package absent.
Toolkits may supply `validate_args(name, args)` to return validated arguments or raise `ToolError` before policy
and approval; otherwise the native argument checker applies. MCP uses full JSON Schema validation with external
schema retrieval disabled, including support for zero-argument schemas and local references.

## Writing a module

Host tools that belong to tower workspaces use `ModuleRuntime.workspace_toolkit(project, defaults, member)`.
The core calls this hook only for tower workspaces; a returned toolkit supplies `schemas()`, `tool_names` and
`call()`. `ModuleRuntime.project_prompt(project, defaults)` can add project-specific guidance to non-chat
sessions. `session_prompt(project, defaults, app)` delegates to `project_prompt` by default and lets modules
with personal context withhold it from Apps. These hooks contribute nothing by default.

1. Make `harness_modules/<name>/__init__.py` with `MODULE = Module(name=..., switches=(...), ...)`. Keep it light:
   point at the heavy parts with small functions that import them on demand.
2. Import the core only from `harness.modules`. If you need a core object it doesn't export, add it to `_PUBLIC`.
3. Put the profile switch in `config.MODULE_NAMES` so profiles and the installer accept it (see below).
4. Add tests next to the module's behaviour, and check `tests/test_modules.py` still passes with your package absent
   (`module_packages: []`).

## What stage (b) leaves in the core

These are names, not imports, and move with the config and storage split in stages (c) and (f):

- Memory library: `MemoryLibraryConfig`, the `memory_library:` YAML section, profile switch, project flag,
  App capability and discovery path remain core names, along with the access and mandatory write-approval
  policy rules. All YAML keys, API paths and `harness memory ...` commands are unchanged; no deprecation is
  needed. The service import moves from `harness.memory_library` to `harness_modules.memory_library.service`.
  The add-on owns the read/write tools, clone refresh, frozen personal profile, owner/admin routes, CLI rows
  and `memory_library.enabled` registry setting. The service is available as `manager.memory_library` instead
  of `manager.runner.memory`. Absent packages contribute no routes, settings, tools or prompts; present but
  switched-off packages keep their management routes and settings. Members never receive the tools and Apps
  never receive the personal profile. Approved writes still require the exact reviewed diff.

- Homelab: `HomelabConfig`, the `homelab:` YAML section, profile switch, project flag, App capability,
  discovery path and restart/rebuild approval rules stay in the core. Configuration keys and agent tool names
  are unchanged. The service import moved from `harness.homelab` to `harness_modules.homelab.service`.
  The add-on owns allowlisted host tools and project guidance, including the scratch-project warning.
  An absent or uninstalled package contributes no tools or prompt. There is no runtime enable setting,
  management route or CLI command for homelab; the existing installer/profile switch selects it.

- `config.py`: the `images:` YAML section (`ImagesConfig`), the `images` / `image_edit` profile switches in
  `MODULE_NAMES` and `ModulesConfig`, `Project.images`, and `DEFAULT_IMAGES_MODELS_DIR`. Config loads before any
  module is discovered and the installer writes these switches, so the core still parses them; it only gives them
  effect when a present module answers to them.
- `db.py` and its migrations: the `images` table.
- `access.py`: members are refused `/images` paths (harmless when the routes are absent).
- `checkpoints.MUTATING_TOOLS` lists `generate_image`; the module also declares it through `ToolGate.mutating`.
- `resources.py` and `endpoint.py` read `manager.images` for the ComfyUI GPU holder and the `/v1` `features.images`
  flag.
- Notifications (`notify:` YAML section and `NotifyConfig`, the `notifications` switch, `/me`'s `notify` block,
  `access.py`'s `/notify` rule) stay in the core as names. Core code that sends a notification (canary, Remote
  Control, the image module) goes through `Manager.notifier`, a stand-in that drops everything while the module is
  absent. The module reaches `harness.jobs.summary` through `harness.modules.job_summary` until jobs is a module.
  The old config keys (`notify.*`, `notifications.enabled`) are unchanged.
- Session search: the FTS5 `search_index` table, its indexing as events are written and `Database.search_events`
  stay in the core (`harness/search_index.py`, `harness/db.py`), because the index lives in the session database and
  backups carry it; events keep being indexed while the module is absent, so installing it later finds old sessions.
  The `search:` YAML section, the `search.enabled` key, `Project.session_search` and the `search` App capability are
  unchanged. The module owns the queries, `/search`, `/api/v1/search`, `session_search` / `session_read`, their prompt
  section and the `harness search` row.
- Backup: the `backup:` YAML section (`BackupConfig`), the `backup` switch, `backup.dir` in discovery paths and
  `harness/sqlite_backup.py` (shared with pre-migration snapshots, which keep working whatever modules are present)
  stay in the core; the old config keys are unchanged, so nothing needs a deprecation. The module owns the nightly
  schedule, the on-demand `POST /maintenance/backup` and `harness maintenance backup`, the `backup.*` settings keys,
  the `backup` entry of `GET /maintenance` (the core reports `{"enabled": false}` while the module is absent), the
  `harness_backup_*` metrics, the doctor check and `python -m harness_modules.backup.restore` (was
  `python -m harness.backup_restore`). While the module is absent no scheduled backup runs and `backup.enabled` has
  no effect; pre-migration snapshots still happen. The image archive joins the backup through
  `ModuleRuntime.backup` as before.
- Skills: the `skills:` YAML section (`SkillsConfig`), the `skills` profile switch, the skill tables and frozen
  session records in `db.py` / migrations, discovery of the reviewer key path, and `access.py`'s `/skills` rule
  remain core names. All configuration keys and `harness skills ...` commands are unchanged; no deprecation is
  needed. The module owns the store, standalone sandbox validator, advisory reviewer and GPU-idle check,
  owner/admin routes, CLI rows, `skills.enabled`, skill metrics, and instruction injection. The manager reads
  `manager.skills` (None while absent/off) and delegates injection to the runtime. `ToolGate.eligible(kit, session)`
  lets the module preserve the proposal tool's session rules instead of using project/App capability flags;
  `mcp=False` preserves its exclusion from hosted MCP tools. Absent skills add no routes, settings, metrics or tools.
  Import paths moved from `harness.skills`, `harness.skill_review`, and `harness.skill_validate` to
  `harness_modules.skills.service`, `harness_modules.skills.skill_review`, and `harness_modules.skills.skill_validate`.
