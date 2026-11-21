# Optional modules

Issue #334 splits Agent Harness Server into a core and optional add-on modules. This page is the module
interface ([`harness/modules.py`](../harness/modules.py)) and how to write a module. The modules so far, in the
order they were extracted (the other optional features move one module per PR in stage (c)):

<!-- generated:begin modules-list -->
- **Images** ([`harness_modules/images/`](../harness_modules/images/)): image generation, edit, upscale and archive
- **Notifications** ([`harness_modules/notifications/`](../harness_modules/notifications/)): ntfy phone notifications
- **Session search** ([`harness_modules/search/`](../harness_modules/search/)): `/search`, `/api/v1/search` and the `session_search` and `session_read` tools
- **Nightly backup** ([`harness_modules/backup/`](../harness_modules/backup/)): backup plus verify and restore (#374)
- **Instruction skills** ([`harness_modules/skills/`](../harness_modules/skills/)): owner-approved instructions
- **Homelab** ([`harness_modules/homelab/`](../harness_modules/homelab/)): allowlisted homelab tools
- **Memory library** ([`harness_modules/memory_library/`](../harness_modules/memory_library/)): personal memory tools, approved writes and frozen session profiles
- **Runners** ([`harness_modules/runners/`](../harness_modules/runners/)): remote transport, native pairing and the Mac bundle
- **Remote control** ([`harness_modules/remote_control/`](../harness_modules/remote_control/)): Claude Remote Control and owner-only folder discovery
- **Endpoint** ([`harness_modules/endpoint/`](../harness_modules/endpoint/)): OpenAI and Anthropic inference, request accounting and the embeddings proxy
- **Local model** ([`harness_modules/local_model/`](../harness_modules/local_model/)): llama-server supervision, warm-up, GPU holds and RAM admission
- **Jobs** ([`harness_modules/jobs/`](../harness_modules/jobs/)): scheduled jobs and templates
- **MCP client** ([`harness_modules/mcp_client/`](../harness_modules/mcp_client/)): owner-pinned stdio MCP tools for the native loop (#260)
- **Hub inventory** ([docs/hub.md](hub.md)): read-only module, paired-app and local-entry inventory for the standalone Hub and CLI
<!-- generated:end modules-list -->

The optional [Hub inventory](hub.md) is read by the standalone Hub app and the CLI.

## Rules

- **One registration point.** A module is a package whose `MODULE` attribute is a `harness.modules.Module`. Everything
  the module adds to the daemon is a field of that object. The core asks `harness/modules.py` for contributions and
  never imports a module package (`tests/test_modules.py` scans `harness/` for it).
- **Modules import the core only through `harness.modules`.** Its `_PUBLIC` table re-exports the core objects a
  module may use (`HarnessError`, `RouteTable`, `require_owner`, `app_auth`, `SettingSpec`, `ToolError`,
  `ModelConfig`, …). A module never imports another module. Both are enforced by a test.
- **Absent means absent.** A module is *present* when its package is discovered and its first service-profile
  switch is installed (`cfg.installed.<switch>`, [#26](service-profile.md)). An absent module (service profile, or
  the package isn't there) adds no routes (they answer 404, or 405 from the static site for other methods), no
  tools, no settings keys, no capability entries, no App scope, and the daemon starts as usual.
- **Present but switched off is not absent.** `images.enabled: false` keeps the module's settings keys (so the owner
  can turn it back on) and its routes, which answer "image generation is disabled" as before.

## Discovery

A configured list: `module_packages` in `config/harness.yaml` (or `harness.local.yaml`), in load order:

```yaml
module_packages: [harness_modules.images, harness_modules.local_model]   # [] runs the core alone
```

Without the key the list is every package under the `harness_modules` namespace, so installing a module is putting
its package there and uninstalling it is removing it. A listed package that fails to import is logged and skipped:
a broken add-on never stops the core.

Why a list and not Python entry points: the Server also runs from a checkout (`python -m harness`, deployed by
fast-forwarding the live checkout), where there is no package metadata for entry points to read. A list is explicit,
ordered and testable (tests pass their own), and the namespace default needs no configuration for an ordinary
install. Installed from wheels ([Packaging](#packaging)), the same namespace scan finds every module wheel, because
each one adds its own package to `harness_modules`.

## Layout

```
harness/                 the core (moves to its own repository in stage f); a regular package
  modules.py             the interface, discovery and ModuleHost
harness_modules/         a PEP 420 namespace package: no __init__.py, so separate distributions can each add one
  images/
    __init__.py          MODULE = Module(...): the registration point; light, the CLI imports it
    runtime.py           ImagesRuntime: lifecycle, GPU hand-over, tools, metrics
    routes.py            owner (/images, image-archive retention) and App API (/api/v1/images) routes
    settings.py          the images.* registry keys
    doctor.py            python -m harness.doctor checks
    service.py edit.py archive.py models.py upscale.py flux_fast.json   (formerly harness/images*.py etc.)
    store.py             ImageStore: the image queries on the core's shared database (see Module stores)
  notifications/         ntfy phone notifications (formerly harness/notify.py)
    __init__.py runtime.py routes.py settings.py service.py
  search/                session search (formerly harness/search.py): /search, /api/v1/search, session_search/read
    __init__.py runtime.py routes.py settings.py service.py
  backup/                nightly backup (formerly Maintenance's backup section) and verify/restore (#374)
    __init__.py runtime.py routes.py settings.py service.py restore.py
  skills/                owner-approved instructions (formerly harness/skills.py, skill_review.py, skill_validate.py)
    __init__.py runtime.py routes.py settings.py service.py skill_review.py skill_validate.py store.py
  memory_library/        personal memory tools, approved writes and frozen session profiles (formerly harness/memory_library.py)
    __init__.py runtime.py routes.py settings.py service.py
  runners/               remote transport, native pairing and Mac bundle (formerly harness/remote.py, mac_client.py)
    __init__.py runtime.py routes.py service.py mac_client.py store.py
  remote_control/        Claude Remote Control and owner-only folder discovery (formerly harness/remote_control.py)
    __init__.py runtime.py routes.py settings.py service.py folder_discovery.py discovery_paths.py discovery_api.py
  endpoint/              OpenAI/Anthropic inference, request accounting and embeddings proxy (formerly harness/endpoint.py)
    __init__.py runtime.py routes.py settings.py service.py metrics.py store.py
  local_model/           llama-server supervision, warm-up, GPU holds and RAM admission (last stage c module)
    __init__.py runtime.py routes.py settings.py service.py warmup.py resources.py metrics.py doctor.py
```

Modules sit beside the core, not inside it, so stage (f) can move `harness/` to the new repository unchanged while
`harness_modules/` stays here as add-on packages: nothing in either tree has to be pulled out of the other, and a
module's only link back is `harness.modules`.

The `local_model` add-on owns both `local_model` and `gpu_guard` profile switches. Warm-up remains available
with `gpu_guard.enabled: false`; the GPU/RAM admission checks are off and their settings remain available.
Removing the package removes `/models/status`, `/models/warm`, their App counterparts, and `/gpu` and `/resources`
management routes. The local backend adapter, model catalog and `backends.local.model` setting stay in core:
they can call an externally supervised server without the add-on. `harness/local_inference.py` holds the shared
state vocabulary and `NoWarmer`, which performs no probes or supervision. Existing YAML and managed overlay
names are unchanged. Stage (c) is complete with this extraction; stages (d)–(g) remain.

Resource wiring runs in `ModuleRuntime.after_init()` after every runtime has initialized, so images receives RAM
admission and lazy loading regardless of discovery order. Exclusive GPU consumers request server control through
`ModuleHost.model_control()` / `ModuleRuntime.model_control()` rather than importing another module. Only the
local-model module supplies that control. Images requires it when actually taking the GPU.

## Packaging

Stage (e) of #334 builds wheels with the in-tree backend `scripts/packaging/agent_harness_build.py`, which uses the
standard library only. `pyproject.toml` declares the layout ([INSTALL.md](INSTALL.md#install-the-server-from-wheels-pip)
has the commands):

- **The core wheel**, `agent-harness`, holds the `harness` package (a regular package, so an installed copy never
  merges with another `harness` folder on the path) and the SDK as the top-level module `harness_client`. Its
  dependencies are `requirements.txt`'s lines, with pytest under the `test` extra because the daemon never imports it.
- **One add-on wheel per module**, `agent-harness-module-<name>`, holding `harness_modules/<name>/` and requiring the
  exact core release. A module is already the unit the interface adds and removes: installing its wheel puts its
  package in the namespace, where discovery finds it, and `pip uninstall` makes it absent. Modules never import each
  other, so each wheel needs only the core, and an install takes only the modules it wants. No wheel ships a
  `harness_modules/__init__.py`, so the namespace stays open to all of them.

A wheel holds the `.py` sources of its package plus the files `pyproject.toml` lists, nothing else. Built from a git
checkout, or from a project folder inside one, the build reads only files git tracks, `pyproject.toml` included, so an
untracked or ignored file never ships. Every read refuses a link anywhere on its path, whether the disk has it or git
records it. A module's own data files go in `package-data` (`images` lists
`flux_fast.json`). A module that reads a repository file outside its package (the hub's manifest schema in `docs/`,
the runners' Mac bundle in `macrunner/`) lists it under `[tool.agent-harness.addons.repo-files]` and reads it with
`harness.modules.repo_file(__package__, "<path>")`. That is the checkout's file when the Server runs from one, and the
copy the wheel carries in `<package>/_repo_files/` when it is installed. Nothing may resolve against the folder above
the package, which is site-packages once installed: `tests/test_checkout_paths.py` fails on it, and the core names the
checkout only through `harness.config.CHECKOUT` (None when installed). `tests/test_packaging.py` installs the wheels in
a new virtual environment and runs the Server from it, with the core alone and then with the add-ons.

The Server image (`Dockerfile`, [INSTALL.md](INSTALL.md#run-the-server-in-docker)) installs the core and every module
wheel. Presence still follows the profile, so a module stays absent until it is switched on.

## What a module contributes

| `Module` field | What the core does with it |
| --- | --- |
| `name`, `switches` | `switches[0]` decides presence; every switch gets a `capabilities.modules.<switch>` entry and a hidden `modules.<switch>` registry key while present. |
| `runtime_enabled(cfg, switch)` | The runtime switch behind a profile switch (`cfg.images.enabled`). `module_effective` = installed AND this. |
| `member_forbidden` | Path prefixes and member refusal messages, applied only while the module is present (including switched off). |
| `runtime(manager, module)` | Builds a `ModuleRuntime` (below) for each Manager. |
| `owner_routes()` | A `RouteTable` installed with the core's daemon routes, behind the same owner/guest/member guard. Handlers call `require_owner` where the route is owner-only. |
| `admin_paths` | Owner routes also served under `/api/admin/v1` (owner credential required) and listed in its `operations`. |
| `app_routes()` | A `RouteTable` of `/api/v1/...` routes. Handlers call `app_auth(request, scope)`. |
| `public_routes()` | Public bootstrap routes, such as the Mac downloads and one-time runner pairing (the pairing code authorizes redemption). |
| `app_scopes`, `app_capabilities` | App token scopes it adds, and `app.capabilities` values (`{capability: scope}`). |
| `tools` (`ToolGate`), `tool_names` | When the runtime's `toolkit()` is offered to a session: project flag, App capability, members, MCP for hosted sessions, whether it writes into the workspace (`workspace_root` / runner `put_bytes`), which tools mutate files (quota and checkpoints), the telemetry span, the system-prompt section (`prompt`) a session gets with it. `tool_names` are reserved against App tools. |
| `settings()` | `SettingSpec`s with defaults, bounds, `enable_check`s and named getters/setters, merged into the registry. |
| `local_cli(groups)` | Adds offline parsers to existing CLI groups, with a `local_handler(args)` default; these run before client configuration and make no daemon request. |
| `cli`, `cli_groups` | Rows in `harness.cli` `ADMIN_COMMANDS` format (`harness images …`); the stage (a) parity rows. The Mac client bundle carries a JSON copy (`mac_client.py`). |
| `principal_capabilities(owner, scopes)` | Entries for `/me` and `/api/v1/me` `capabilities`. |
| `doctor(report, cfg)` | Checks for `python -m harness.doctor`. |
| `docs` | Pointers to the module's documentation. |

`ModuleRuntime` hooks, all optional:

`status()` returns a secret-free, read-only detail dictionary, or `None` (the default) for presence
only. It may be synchronous or async. Synchronous hooks run off the event loop; async hooks have a six-second
budget. Hub inventory skips switched-off modules and catches hook failures as `status: {state: error}` without
logging exception contents. Hooks must not wake models, mutate daemon state, or expose tokens, paths to secrets,
payloads, or other app data. Local model reports loaded/warming state, runners online count, backup last success,
and notifications enabled/queued counts. No module imports another module; `_PUBLIC` is unchanged.


| Hook | When |
| --- | --- |
| `__init__` | In `Manager.__init__`, before Maintenance. Set `self.backup` to join the nightly backup (images: the image archive). |
| `init()` | After the managed settings overlay is applied: create services here (`self.service`). |
| `wire_resources(guard, warmer)` | The resource guard is on: take its RAM check and lazy-load preference. |
| `start()` / `stop()` | Daemon start and shutdown. |
| `toolkit()` | The object with `tool_names`, `schemas()` and `call()`; offered per `Module.tools`. |
| `gpu_taken`, `busy()`, `gpu_hold()`, `gpu_resume(ids)` | GPU interaction: the model warmer stays parked while `gpu_taken`; skill review waits while `busy()`; the GPU guard's pause and resume. |
| `features()`, `app_root()` | `/api/v1` `features` entries and extra top-level keys (`image_modes`). |
| `gpu_holders()` | GPU-holder names for machine diagnostics; the core aggregates all present runtimes. |
| `metrics(out, db)` | Prometheus lines on `/metrics`. |
| `key_request_counts(key_ids)` | `{key id: logged requests}` for the `/keys` listing's `requests`; `ModuleHost` sums the answers and `SessionStores` adds them to each listed key (0 for none). The endpoint module answers it. |
| `scheduled_job(jid)` | The scheduled job `jid` (`name`, `notify` and the rest), from the module that schedules jobs; `ModuleHost.scheduled_job` asks each runtime. Notifications names a finished run's job through it, so it never imports the jobs module; with jobs absent the answer is None and the notification names the session. |

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
   point at the heavy parts with `lazy(".routes", "owner_routes")` (or `lazy(".runtime", "MyRuntime",
   call=True)` for a hook that calls its target), which imports the submodule only when the core calls the hook, and
   use `enabled_flag("<section>")` for a `runtime_enabled` that reads `cfg.<section>.enabled`. Add the module's row
   to `tests/test_module_hooks.py`.
2. Import the core only from `harness.modules`. If you need a core object it doesn't export, add it to `_PUBLIC`.
   Put the queries on the module's own tables in its store (see [Module stores](#module-stores)), not in
   `harness/db.py`.
3. Put the profile switch in `config.MODULE_NAMES` so profiles and the installer accept it (see below).
4. Add one row to the table in `tests/test_module_lifecycle.py`: its probe route, settings keys, capability, routes,
   CLI row, metrics and tools. The table runs every module on, off, absent and uninstalled through `create_app`, so the
   lifecycle needs no test file of its own. Add tests next to the module's behaviour for the rest.
5. The package becomes its own wheel with no further setup. List any data file that isn't a `.py` source in
   `pyproject.toml` (`package-data`, or `repo-files` for a repository file outside the package; see
   [Packaging](#packaging)); `tests/test_packaging.py` fails on a tracked file that no wheel ships.

## Module stores

A module's queries on its own tables are methods of its store, `harness_modules/<name>/store.py`, never of the core
`Database` (#992). A store subclasses `harness.modules.ModuleStore` and decorates each method with
`harness.modules.database_reads` or `database_writes`, as `Database` does its own: a read runs on a pooled read-only
connection, a write runs on the core's writer thread as one transaction, and inside a caller's `write` it joins that
transaction. `Store(db)` takes the manager's database (or a lone `Database`) and wraps the main one, so
`await store.aio.<method>()` works too. A store forwards nothing else: it has no `get_session` or any other core method.
A store method that must also call a core method in its transaction calls it on `self.core` (the runner store's
redeem mints the owner key with `create_api_key`). A store is cheap to build, so a route can make one per request.

| Module | Store | Tables |
| --- | --- | --- |
| skills | `SkillRecordStore` (the service is `SkillStore`) | `skill_proposals`, `skill_installed`, `skill_versions`, `skill_project_allowlist`, `skill_rejected`, `skill_review_jobs` |
| jobs | `JobStore` | `jobs`, `templates` |
| runners | `RunnerPairingStore` | `runner_pairing_codes` |
| endpoint | `EndpointRequestStore` | `endpoint_requests` (the request log) |
| images | `ImageStore` | `images` |

The tables themselves stay in the core's frozen baseline and migrations, so existing installs and backups keep their
schema while a module is absent (#334). `SessionStore.job_sessions` stays in the core: it reads a scheduled job's sessions,
which are session rows on Web's store, not job rows. The `/keys` listing's per-key `requests` count is the endpoint
module's (`ModuleRuntime.key_request_counts`, filled in by `SessionStores` on the list, the one-key read and an undone
revoke); it is 0 while the module is absent. `tests/test_module_stores.py` fails on any `Database` method whose SQL
names a module table, and on any store that forwards attributes to the Database.

The core's own queries split the same way, one domain store per change (#992 stage 5). A core store subclasses
`harness.db.CoreStore`, and `Database` builds it and exposes it as an attribute:

- **`db.credentials`** (`harness/credential_store.py`): API keys, pairing codes, pairing requests and the Hub claim,
  stream tickets, and App provider credentials.
- **`db.sessions`** (`harness/session_store.py`): the `sessions` table and the rows keyed by one session. That means
  checkpoints, artifacts, review comments, secret dismissals, events and the search index, approvals, and smart
  reviews. It also holds the other rows each App keeps in its own store: its tool calls, end users, idempotency keys
  and namespace audit (#992 stage 6). Every Database builds its own: the main store, Web's, and each App's.
- **`db.accounts`** (`harness/account_store.py`): household accounts and the account audit, member projects, GitHub
  connections, members' own provider keys, Google identities and invitations, and Stables web sessions.
- **`db.apps`** (`harness/app_store.py`): the main store's per-App records beside the App registry. That means each
  App's settings, the session caps the owner set, its retention, and the erasure of a revoked App. Erasing an App is
  one transaction across stores on purpose: the tombstone and provider credentials (CredentialStore's tables), the
  settings and the meta keys commit together or not at all.
- **`db.usage`** (`harness/usage_store.py`): the usage log (`usage`) and each hosted backend's reported usage
  (`backend_usage`), both in the main store.
- **Still on `Database`:** `meta`, which stays there as infrastructure.

Every caller calls the store: `db.credentials.get_api_key(kid)`, `await db.credentials.aio.create_api_key(...)`.
`Database` forwards nothing. The temporary forwarding shim that stage 5 added while call sites moved was deleted in
stage 6 (#992). `tests/test_database_shim.py` checks that `Database` has only its infrastructure and `meta`, and a
source scan there fails on a call to any store method that isn't made on that store (for sessions, a routing proxy).
`operation_audit` writes through the account store of the database it is given.

Session methods go through a routing proxy, and SessionStores adds its own key listings:

- **Session methods.** `SessionStores` routes each session to its own store. On the routing wrappers (`m.db`,
  `scope(app_id)`, and `for_app` or `for_session` for an App), `sessions` is the one per-session routing proxy
  (`app_stores._RoutedSessions`, #992 stage 6). It has `SessionStore`'s methods, and each call runs on the store
  that holds the rows, held for that call only:
  - `m.db.sessions` routes by session id, and still refuses a write about an erased App session. It runs
    SessionStores' own overrides, such as the index upkeep and the multi-store lists, and sends every other
    method to Web's store. The main store holds no sessions once they have moved there.
  - `for_app(app_id).sessions` runs the index-keeping writes (`insert_session`, `update_session`,
    `delete_session` and `insert_approval`) through SessionStores, so the index stays current.
  - `for_app(app_id).sessions` runs on that App's store. Call an App's end users, idempotency keys and namespace
    audit there, since they take no session id. `m.db.sessions` refuses them, because it can't tell whose rows they
    are.
  - `scope(app_id).sessions` narrows lists, searches and id lookups to what that App, or the owner for `""`, may see.

  `core_store("sessions")` still refuses, so a wrapper without its own proxy fails instead of misrouting. A Web route
  (`for_app("")`, `for_app(WEB_APP_ID)`, or `for_session` of an owner or member session) returns Web's Database
  itself, so its `sessions` is Web's own store: the one that holds those sessions. Events and search, checkpoints,
  artifacts, review comments, secret dismissals, approvals and smart reviews, and the session rows, lists and counts
  (`get_session`, `update_session`, `list_sessions`, `count_sessions` and the rest) are called only through a
  `sessions` store or proxy: `Database` forwards no session method. A wrapper asked for a `SessionStore` method
  itself (`m.db.get_session`, `for_app(app_id).insert_session`, `scope(app_id).list_sessions`) raises, pointing at
  its `sessions` proxy. SessionStores' own overrides (`m.db.list_sessions`) are real methods the proxy runs, so
  they still answer; a scan fails on any call to a `SessionStore` method that isn't made on a `sessions` store or
  proxy. Every route, and
  every Database that `SessionStores` opens, hands out the main store's `credentials`, `accounts`, `apps` and
  `usage`. `app_stores._STORE_HOME` records where each store's rows live, and a new store must add itself there.
- **Key listings.** `SessionStores.listed_api_keys()`, `listed_api_key(kid)` and `restore_listed_api_key(kid)` are
  the key list as Settings and the Hub show it: `CredentialStore`'s rows with each key's request count and, for an
  App, its store's metadata, which only SessionStores can read. Call them on `m.db`. Their names differ from the
  store's (`list_api_keys`, `get_api_key_listing`, `restore_api_key`), so a credential name is only ever the store's.

## What stage (b) leaves in the core

These are names, not imports, and move with the config and storage split in stages (c) and (f):

- Remote Control: `RemoteControlConfig`, discovery configuration, the `remote_control:` YAML section
  (especially `remote_control.folders` in `harness.local.yaml`), installer/profile switch, access rules,
  mandatory approval policy and settings audit redaction remain core names. YAML keys, registry keys,
  API URLs and `harness remote-control ...` commands are unchanged; no deprecation is needed.
  `harness.remote_control` becomes `harness_modules.remote_control.service`; `folder_discovery`,
  `discovery_paths` and `discovery_api` also move into that package. The add-on owns services, tools,
  routes, settings and validators, scopes, discovery limits and CLI rows. Absent packages contribute
  none; present but disabled packages keep their settings and routes. Remote Control servers still
  survive daemon shutdown. Module registration never launches a connection.

  `Module.register_admin(app, mgr, require_admin)` registers owner-only admin operations and returns
  inventory rows. `settings_validators` and `settings_change_validators` contribute registry checks;
  `settings_schema` adds admin schema metadata. `SettingSpec.normalize_change(cfg, value)` validates
  and canonicalizes only incoming changes, so startup setters remain independent of the filesystem.

- Runners: `RunnerConfig`, the `runners:` YAML section and profile switch, remote project targets, token-file
  discovery, pairing-code database tables/methods, access rules and compatibility metadata remain core names.
  `harness/runner_contract.py` provides shared failure types, a remote-workspace marker and the local-only
  `NoRunnerHub` fallback. The add-on owns transport, remote workspaces/sandboxes, native pairing, Mac downloads,
  admin routes, capabilities and CLI rows. Core session execution and maintenance call the runtime's hub;
  absent/uninstalled runners add no routes, settings or capabilities and remote starts fail before allocating
  a workspace. Runners retain their installer-only switch (there is no new runtime enable setting).
  YAML keys, registry keys, commands, URLs, bearer tokens and runner protocol versions are unchanged, so existing
  paired clients need no re-pairing. Imports move from `harness.remote` and `harness.mac_client` to
  `harness_modules.runners.service` and `harness_modules.runners.mac_client`. The standalone `macrunner/`
  payload and the shared CLI/updater stay at their existing paths; bundle command rows include the add-on.

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
- `db.py` and its frozen baseline migrations retain the historical `images` table and columns, so existing
  databases and backups keep their schema even while the add-on is absent. Live image queries moved to
  `harness_modules/images/store.py`: `ImageStore(db)` is a [module store](#module-stores). Database image methods
  are now methods of that store. The module's service and archive wrap the shared database.
- Images owns its member access rules through `Module.member_forbidden`, the `generate_image` mutation name
  through `ToolGate.mutating`, and the ComfyUI holder through `ModuleRuntime.gpu_holders`. Resources aggregates
  holders, and endpoint aggregates `features()`, without naming images. An absent package contributes none of them;
  a present disabled package still refuses member access and reports false image features.
- Endpoint: `EndpointConfig`, the `endpoint:` YAML section and profile switch, the shared `InferenceGate` and the
  `endpoint_requests` table's schema remain core names. The module's `EndpointRequestStore` writes the request log and
  counts each key's requests for the `/keys` listing (#992); while the module is absent that count is 0. `/keys`, its admin API and CLI
  stay in core because Apps and owner credentials need them without inference. All URLs, auth, response shapes,
  configuration keys and CLI commands are unchanged; no deprecation is needed. The add-on owns the `/v1` inference
  and discovery routes, embeddings proxy, request logging, `endpoint.*` settings, queue initialization and
  `harness_endpoint_*` metrics. Absent/uninstalled endpoint packages contribute no inference routes, settings or
  metrics; a present but disabled module retains settings and the existing disabled responses. The import path
  moved from `harness.endpoint` to `harness_modules.endpoint.service`. Its discovery features include contributions
  from all present modules through `ModuleHost.features()`.
- Notifications (`notify:` YAML section and `NotifyConfig`, the `notifications` switch, `/me`'s `notify` block,
  `access.py`'s `/notify` rule) stay in the core as names. Core code that sends a notification (canary, Remote
  Control, the image module) goes through `Manager.notifier`, a stand-in that drops everything while the module is
  absent. The module reaches the shared result protocol through `harness.modules.job_summary`.
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
- Skills: the `skills:` YAML section (`SkillsConfig`), the `skills` profile switch, the skill tables' schema and frozen
  session records in `db.py` / migrations (their queries are the module's `SkillRecordStore`, #992), discovery of the reviewer key path, and `access.py`'s `/skills` rule
  remain core names. All configuration keys and `harness skills ...` commands are unchanged; no deprecation is
  needed. The module owns the store, standalone sandbox validator, advisory reviewer and GPU-idle check,
  owner/admin routes, CLI rows, `skills.enabled`, skill metrics, and instruction injection. The manager reads
  `manager.skills` (None while absent/off) and delegates injection to the runtime. `ToolGate.eligible(kit, session)`
  lets the module preserve the proposal tool's session rules instead of using project/App capability flags;
  `mcp=False` preserves its exclusion from hosted MCP tools. Absent skills add no routes, settings, metrics or tools.
  Import paths moved from `harness.skills`, `harness.skill_review`, and `harness.skill_validate` to
  `harness_modules.skills.service`, `harness_modules.skills.skill_review`, and `harness_modules.skills.skill_validate`.

- Jobs: `JobsConfig`, the `jobs:` YAML section and profile switch, the jobs/templates tables' schema (their queries
  are the module's `JobStore`, #992), `SessionStore.job_sessions`, scheduled-session fields, and the `/jobs` and `/templates` access rules remain core names.
  `harness/job_results.py` is the shared STATUS and notification-summary protocol: the runner still records
  results for existing scheduled sessions even when the jobs package is absent, and notifications does not
  import the jobs add-on: it reads a run's job through `ModuleHost.scheduled_job`. Scheduling, owner/admin routes, settings and CLI rows live in `harness_modules/jobs`.
  The service import moves from `harness.jobs` to `harness_modules.jobs.service`. YAML keys, registry keys,
  API paths and `harness jobs ...` / `harness templates ...` commands are unchanged; no deprecation is needed.
  An absent/uninstalled package contributes no routes, settings or capabilities. A present package with
  `jobs.enabled: false` keeps cron preview, template management and registry settings; scheduling is disabled.

Toolkits may expose `mutating_tools`, a collection of full tool names evaluated for the current session.
The host combines it with `ToolGate.mutating` for native checkpoints and workspace quota enforcement.
MCP client toolkits conservatively mark every tool on a server with a `rw` workspace grant as mutating.

## Memory library clone health

The memory library keeps its own git clone (`memory_library.clone_dir`). Agents and people must change it only through
the module's write path (approved `memory_edit`/`memory_write`, or the Settings profile editor); edits made directly in
the clone are never discarded or committed automatically.

A refresh reports one of `ok`, `dirty` (uncommitted or untracked files), `diverged` (local commits and new remote
commits) or `failed`. `GET /memory` returns `refresh_state`, `changed_paths` (names only), `refresh_failures` and
`last_success`. `/metrics` exposes `harness_memory_library_refresh_ok`, `..._refresh_state`, `..._refresh_failures`,
`..._changed_paths` and `..._last_success_age_seconds`. With notifications on, three failed refreshes in a row send
one ntfy alert per episode, which clears when a refresh succeeds. Writes are refused while the clone has uncommitted
changes.
