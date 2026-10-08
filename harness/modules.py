"""Optional modules: the one registration point between the core and add-on packages (#334, docs/modules.md).

A module is a Python package whose ``MODULE`` attribute is a :class:`Module`. It contributes routes, agent tools,
settings keys, capability entries, CLI rows, doctor checks and lifecycle hooks through that one object; the core
asks this file for them and never imports a module package itself.

Discovery is a configured list: ``module_packages`` in harness.yaml (or profile.yaml). When the key is absent the
list is every package under the ``harness_modules`` namespace, so installing a module is putting its package there
and uninstalling it is taking it away. A module is *present* for a config when its package is discovered and its
first service-profile switch (#26) is installed (``cfg.installed``); an absent module contributes nothing at all.
A present module that is switched off at runtime (``images.enabled: false``) keeps its settings keys, so the owner
can turn it back on, and its routes answer "disabled" as before.

Modules import the core only through this file: the names in ``_PUBLIC`` resolve lazily to the core objects a
module may use. A module never imports another module.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("harness.modules")

NAMESPACE = "harness_modules"

# The core's public interface for modules: name -> (core module, attribute). Resolved on first use, so importing
# harness.modules never drags in the API or the manager.
_PUBLIC = {
    "GpuGuardConfig": ("harness.config", "GpuGuardConfig"),
    "ACTIVE": ("harness.runner", "ACTIVE"),
    "doctor_run": ("harness.doctor", "run"),
    "EXPECTED_WAKE_SECONDS": ("harness.local_inference", "EXPECTED_WAKE_SECONDS"),
    "LOW_MEMORY": ("harness.local_inference", "LOW_MEMORY"),
    "UNREACHABLE": ("harness.local_inference", "UNREACHABLE"),
    "PAUSED": ("harness.local_inference", "PAUSED"),
    "WAKING": ("harness.local_inference", "WAKING"),
    "READY": ("harness.local_inference", "READY"),
    "database_reads": ("harness.db", "_reads"),
    "database_writes": ("harness.db", "_writes"),
    "AsyncDatabase": ("harness.db", "AsyncDatabase"),
    "GpuExclusive": ("harness.scheduler", "GpuExclusive"),
    "QueueFull": ("harness.scheduler", "QueueFull"),
    "SLEEPING": ("harness.local_inference", "SLEEPING"),
    "UNLOADED": ("harness.local_inference", "UNLOADED"),
    "RemoteControlConfig": ("harness.config", "RemoteControlConfig"),
    "PROJECT_NAME": ("harness.config", "PROJECT_NAME"),
    "Envelope": ("harness.managed_config", "Envelope"),
    "ManagedConfigError": ("harness.managed_config", "ManagedConfigError"),
    "parse_envelope": ("harness.managed_config", "parse_envelope"),
    'RunnerConfig': ('harness.config', 'RunnerConfig'),
    'ToolOutputConfig': ('harness.config', 'ToolOutputConfig'),
    'runner_compatibility': ('harness.compat', 'runner_compatibility'),
    'PROTOCOLS': ('harness.compat', 'PROTOCOLS'),
    'BUILD_ID': ('harness.compat', 'BUILD_ID'),
    'CLIENT_PROTOCOLS': ('harness.compat', 'CLIENT_PROTOCOLS'),
    'MAC_CLIENT_VERSION': ('harness.compat', 'MAC_CLIENT_VERSION'),
    'compat': ('harness.compat', None),
    'API_VERSION': ('harness.apps', 'API_VERSION'),
    'require_admin': ('harness.admin', 'require_admin'),
    'log_safe': ('harness.api', '_log_safe'),
    'FILE_TOOLS': ('harness.fileops', 'FILE_TOOLS'),
    'MAX_PUT_BYTES': ('harness.fileops', 'MAX_PUT_BYTES'),
    'shell_result': ('harness.tools', 'shell_result'),
    'tool_schemas': ('harness.tools', 'tool_schemas'),
    'ToolOutput': ('harness.verify', 'ToolOutput'),
    'run_verify': ('harness.verify', 'run_verify'),
    'RunnerError': ('harness.runner_contract', 'RunnerError'),
    'RunnerOffline': ('harness.runner_contract', 'RunnerOffline'),
    'RemoteWorkspaceContract': ('harness.runner_contract', 'RemoteWorkspace'),
    "MemoryLibraryConfig": ("harness.config", "MemoryLibraryConfig"),
    "truncate_middle": ("harness.fileops", "truncate_middle"),
    "write_text_within": ("harness.fileops", "write_text_within"),
    "HomelabConfig": ("harness.config", "HomelabConfig"),
    "app_allows": ("harness.settings", "app_allows"),
    "SkillsConfig": ('harness.config', 'SkillsConfig'),
    "ModelConfig": ('harness.config', 'ModelConfig'),
    "resolve_path": ('harness.fileops', 'resolve_path'),
    "contained": ('harness.storage', 'contained'),
    "is_reparse_point": ('harness.storage', 'is_reparse_point'),
    "default_chat": ('harness.llm', 'chat'),
    "Completion": ('harness.llm', 'Completion'),

    "Config": ("harness.config", "Config"),
    "validate_mcp_servers": ("harness.mcp_config", "validate_servers"),
    "ImagesConfig": ("harness.config", "ImagesConfig"),
    "DEFAULT_IMAGES_MODELS_DIR": ("harness.config", "DEFAULT_IMAGES_MODELS_DIR"),
    "load_config": ("harness.config", "load"),
    "module_effective": ("harness.config", "module_effective"),
    "resolve_images_models_dir": ("harness.config", "resolve_images_models_dir"),
    "Database": ("harness.db", "Database"),
    "SEARCH_TOOLS": ("harness.search_index", "SEARCH_TOOLS"),
    "scoped_store": ("harness.app_stores", "scoped"),
    "TOOLS_ONLY": ("harness.policy", "TOOLS_ONLY"),
    "frozen_app_defaults": ("harness.settings", "frozen_app_defaults"),
    "use_live_app_settings": ("harness.settings", "use_live_app_settings"),
    "ToolError": ("harness.fileops", "ToolError"),
    "run_cmd": ("harness.sandbox", "run_cmd"),
    "HarnessError": ("harness.manager", "HarnessError"),
    "RouteTable": ("harness.api", "RouteTable"),
    "require_owner": ("harness.api", "require_owner"),
    "app_auth": ("harness.apps", "auth"),
    "calling_app": ("harness.apps", "calling_app"),
    "SESSIONS_ALL": ("harness.apps", "SESSIONS_ALL"),
    "owner_id": ("harness.api", "owner_id"),
    "SettingSpec": ("harness.settings", "SettingSpec"),
    "Bounds": ("harness.settings", "Bounds"),
    "module_installed": ("harness.settings", "module_installed"),
    "setting_int": ("harness.settings_keys", "_int"),
    "setting_float": ("harness.settings_keys", "_float"),
    "setting_bool": ("harness.settings_keys", "_bool"),
    "job_summary": ("harness.job_results", "summary"),
    "parse_job_status": ("harness.job_results", "parse_status"),
    "require_url": ("harness.settings_keys", "_require_url"),
    "require_readable_file": ("harness.settings_keys", "_require_readable_file"),
    "migrations": ("harness.migrations", None),
    "storage": ("harness.storage", None),
    "APP_STORE_FILE": ("harness.app_stores", "APP_STORE_FILE"),
    "WEB_APP_ID": ("harness.app_stores", "WEB_APP_ID"),
    "app_dir": ("harness.app_stores", "app_dir"),
    "remove_tree": ("harness.maintenance", "remove_tree"),
    "backup_sqlite": ("harness.sqlite_backup", "backup_sqlite"),
    "OWNER_USER_ID": ("harness.principal", "OWNER_USER_ID"),
    "ROOT": ("harness.config", "ROOT"),
    "ManagedStore": ("harness.managed_config", "ManagedStore"),
}


def __getattr__(name: str):
    target = _PUBLIC.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # Not cached: patches to a public core attribute reach its consumers too.
    module = importlib.import_module(target[0])
    return module if target[1] is None else getattr(module, target[1])


@dataclass(frozen=True)
class ToolGate:
    """When the core offers a module's toolkit to a session, and how it calls it."""
    project_flag: str             # Project attribute that enables the toolkit for a project (``images``)
    capability: str               # app.capabilities value that can narrow it away for an App's sessions
    members: bool = False         # offered to member sessions
    mcp: bool = True              # served to hosted Claude Code sessions over MCP
    workspace: bool = False       # the handler writes into the session workspace (workspace_root / put_bytes)
    mutating: tuple[str, ...] = ()  # tool names that change workspace files (checkpoints, review)
    span: str = ""                # telemetry span name for a call (default: the core's sandbox span)
    prompt: str = ""              # system-prompt section for a session that gets the toolkit
    eligible: Callable[[Any, dict], bool] | None = None  # toolkit/session eligibility instead of project/app flags
    per_session: bool = False    # runtime.session_toolkit(session), after async prepare_session(session)


@dataclass(frozen=True)
class Module:
    """Everything one optional module contributes. Every field but ``name`` and ``switches`` is optional."""
    name: str
    switches: tuple[str, ...]     # service-profile switches it answers to; ``switches[0]`` decides presence
    title: str = ""
    docs: tuple[str, ...] = ()
    # The runtime switch behind each profile switch (``cfg.images.enabled``): installed AND this = effective.
    runtime_enabled: Callable[[Any, str], bool] | None = None
    # Lifecycle: a ModuleRuntime subclass, built with the Manager (see ModuleRuntime).
    runtime: Callable[[Any, "Module"], "ModuleRuntime"] | None = None
    # Routes. Each callable returns a harness.api.RouteTable; the core installs it on the matching surface.
    owner_routes: Callable[[], Any] | None = None   # daemon routes (/images …) behind the owner/guest/member guard
    admin_paths: frozenset[str] = frozenset()       # owner routes also served under /api/admin/v1
    member_forbidden: tuple[tuple[tuple[str, ...], str], ...] = ()  # path prefixes and refusal detail
    app_routes: Callable[[], Any] | None = None     # App API (/api/v1/…); handlers call app_auth for their scope
    public_routes: Callable[[], Any] | None = None  # public bootstrap routes (downloads, code redemption)
    app_scopes: dict[str, str] = field(default_factory=dict)        # App token scopes it adds (``images``)
    app_capabilities: dict[str, str] = field(default_factory=dict)  # app.capabilities value -> the scope it needs
    # Agent tools: the runtime's ``toolkit()`` supplies schemas and handlers; this says when it is offered.
    tools: ToolGate | None = None
    tool_names: tuple[str, ...] = ()                # reserved: App tools may not take these names
    # Settings: SettingSpecs (with defaults, bounds and enable checks) the module owns in the registry.
    settings: Callable[[], list] | None = None
    register_admin: Callable | None = None  # owner-only admin registration (app, mgr, require_admin)
    settings_validators: tuple[Callable, ...] = ()
    settings_change_validators: tuple[Callable, ...] = ()
    settings_schema: dict[str, Any] = field(default_factory=dict)
    # CLI: rows in the harness.cli OPERATIONS format, and group help for their first word.
    cli: tuple[tuple, ...] = ()
    cli_groups: dict[str, str] = field(default_factory=dict)
    # /me and /api/v1/me capabilities: (owner, scopes) -> {name: bool}.
    principal_capabilities: Callable[[bool, frozenset], dict] | None = None
    # `python -m harness.doctor`: (report, cfg) -> None, reporting through report.ok / warn / fail.
    doctor: Callable[[Any, Any], None] | None = None


class ModuleRuntime:
    """One present module's live objects inside a Manager.

    Built in ``Manager.__init__`` before Maintenance (so ``backup`` can join the nightly backup); ``init`` runs
    after the managed settings overlay is applied, so runtime switches are final there. Every hook is optional.
    """

    def __init__(self, manager, module: Module):
        self.manager = manager
        self.module = module
        self.service: Any = None  # the module's main object, None while it is switched off (manager.<name>)
        self.backup: Any = None   # a backup participant for Maintenance (enabled/reconcile/health), or None

    @property
    def cfg(self):
        return self.manager.cfg

    def effective(self, switch: str | None = None) -> bool:
        from .config import module_effective
        return module_effective(self.cfg, switch or self.module.switches[0])

    # lifecycle
    def init(self) -> None:
        """Create services. Runs once, after the settings overlay."""

    def after_init(self) -> None:
        """Wire services after every module has initialized."""

    def model_control(self):
        """Optional supervised model control for exclusive GPU consumers."""
        return None

    def wire_resources(self, guard, warmer) -> None:
        """The resource guard is on: take its RAM check and lazy-load preference."""

    def start(self) -> None:
        """Daemon start, on the event loop."""

    async def stop(self) -> None:
        """Daemon shutdown."""

    # agent tools (see Module.tools): an object with tool_names, schemas() and call(name, args, ...)
    def toolkit(self) -> Any:
        return None

    def workspace_toolkit(self, project, defaults, member):
        """Optional host toolkit for a tower workspace; None when this session is ineligible."""
        return None

    def project_prompt(self, project, defaults) -> str:
        """Additional project guidance for a non-chat session."""
        return ""

    def session_prompt(self, project, defaults, app) -> str:
        """Project guidance with the calling App, so private context can be withheld from Apps."""
        return self.project_prompt(project, defaults)

    async def prepare_session(self, session: dict) -> None:
        """Prepare session-specific tools before the native loop's first tool listing."""

    def session_toolkit(self, session: dict) -> Any:
        return None

    def owns_toolkit(self, kit) -> bool:
        return self.toolkit() is kit

    async def end_session(self, sid: str) -> None:
        """Release session resources on completion, failure or cancellation."""

    # GPU and resources: a module that takes the whole GPU (and stops the language model) reports it here.
    @property
    def gpu_taken(self) -> bool:
        return False

    def busy(self) -> bool:
        """Work queued or running: background GPU work (skill review) waits for it."""
        return False

    def gpu_holders(self) -> tuple[str, ...]:
        """Names shown in machine diagnostics while this module owns the GPU."""
        return ()

    def gpu_hold(self) -> None:
        """The GPU guard paused: stop taking the GPU."""

    def gpu_resume(self, session_ids) -> None:
        """The hold ended: run after the sessions waiting at the time."""

    # capability discovery (cfg.capabilities()["modules"] lists each present module's switches by itself)
    def features(self) -> dict:
        """/api/v1 and /v1 ``features`` entries."""
        return {}

    async def app_root(self) -> dict:
        """Extra top-level /api/v1 keys."""
        return {}

    def metrics(self, out, db) -> None:
        """Prometheus lines for /metrics (out.metric(name, type, help, rows))."""


# discovery
_discovered: dict[tuple[str, ...] | None, tuple[Module, ...]] = {}


def namespace_packages() -> tuple[str, ...]:
    """Every package under the harness_modules namespace, by name."""
    spec = importlib.util.find_spec(NAMESPACE)
    if spec is None or spec.submodule_search_locations is None:
        return ()
    return tuple(sorted(f"{NAMESPACE}.{info.name}" for info in pkgutil.iter_modules(spec.submodule_search_locations)
                        if info.ispkg))


def discover(packages=None) -> tuple[Module, ...]:
    """The modules in ``packages`` (None: the namespace), in order. A package that fails to import is logged and
    left out, so a broken add-on never stops the core."""
    key = tuple(packages) if packages is not None else None
    if key in _discovered:
        return _discovered[key]
    found: list[Module] = []
    for name in (key if key is not None else namespace_packages()):
        try:
            module = getattr(importlib.import_module(name), "MODULE")
        except Exception:
            log.exception("module package %s did not load; running without it", name)
            continue
        if not isinstance(module, Module):
            log.error("module package %s has no MODULE = harness.modules.Module(...); skipped", name)
            continue
        if any(module.name == other.name for other in found):
            log.error("module %s is listed twice; the second (%s) is skipped", module.name, name)
            continue
        found.append(module)
    _discovered[key] = tuple(found)
    return _discovered[key]


def discovered(cfg) -> tuple[Module, ...]:
    return discover(getattr(cfg, "module_packages", None))


def claims(cfg, switch: str) -> Module | None:
    """The discovered module that answers to a service-profile switch, if any."""
    return next((module for module in discovered(cfg) if switch in module.switches), None)


def is_present(cfg, module: Module) -> bool:
    from .settings import module_installed
    return module_installed(cfg, module.switches[0])


def present(cfg) -> tuple[Module, ...]:
    return tuple(module for module in discovered(cfg) if is_present(cfg, module))


def present_switches(cfg) -> tuple[str, ...]:
    return tuple(switch for module in present(cfg) for switch in module.switches)


def absent_switches(cfg, known: tuple[str, ...]) -> frozenset[str]:
    """Profile switches in ``known`` that no present module answers to."""
    return frozenset(known) - frozenset(present_switches(cfg))


def settings_specs(cfg) -> list:
    return [spec for module in present(cfg) if module.settings for spec in module.settings()]


def cli_rows(cfg=None) -> tuple[tuple, ...]:
    """CLI rows of the discovered modules (the CLI talks to a daemon whose config it may not have)."""
    modules = discovered(cfg) if cfg is not None else discover()
    return tuple(row for module in modules for row in module.cli)


def cli_groups(cfg=None) -> dict[str, str]:
    modules = discovered(cfg) if cfg is not None else discover()
    return {group: text for module in modules for group, text in module.cli_groups.items()}


def app_scopes(cfg) -> dict[str, str]:
    return {scope: text for module in present(cfg) for scope, text in module.app_scopes.items()}


def app_capabilities(cfg) -> tuple[str, ...]:
    return tuple(name for module in present(cfg) for name in module.app_capabilities)


def principal_capabilities(cfg, owner: bool, scopes=()) -> dict:
    scopes = frozenset(scopes)
    return {name: value for module in present(cfg) if module.principal_capabilities
            for name, value in module.principal_capabilities(owner, scopes).items()}


def install_routes(app, cfg, surface: str) -> list[Module]:
    """Install the present modules' route tables for one surface (owner, app or public) on a FastAPI app."""
    installed = []
    for module in present(cfg):
        build = {"owner": module.owner_routes, "app": module.app_routes, "public": module.public_routes}[surface]
        if build is not None:
            build().install(app)
            installed.append(module)
    return installed


def admin_paths(cfg) -> frozenset[str]:
    return frozenset(path for module in present(cfg) for path in module.admin_paths)


def member_forbidden(cfg, path: str) -> str | None:
    """Present add-ons own their member access rules, including while switched off."""
    return next((detail for module in present(cfg) for prefixes, detail in module.member_forbidden
                 if any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)), None)


class ModuleHost:
    """The present modules' runtimes for one Manager, in discovery order."""

    def __init__(self, manager):
        self.manager = manager
        self.runtimes: dict[str, ModuleRuntime] = {}
        for module in present(manager.cfg):
            make = module.runtime or ModuleRuntime
            self.runtimes[module.name] = make(manager, module)

    def __contains__(self, name: str) -> bool:
        return name in self.runtimes

    def __iter__(self):
        return iter(self.runtimes.values())

    def get(self, name: str) -> ModuleRuntime | None:
        return self.runtimes.get(name)

    def model_control(self):
        return next((control for rt in self if (control := rt.model_control()) is not None), None)

    def backup_participant(self):
        """Maintenance takes one backup participant (the image archive today)."""
        return next((rt.backup for rt in self if rt.backup is not None), None)

    def init(self) -> None:
        for rt in self:
            rt.init()
        for rt in self:
            rt.after_init()

    def wire_resources(self, guard, warmer) -> None:
        for rt in self:
            rt.wire_resources(guard, warmer)

    def start(self) -> None:
        for rt in self:
            rt.start()

    async def stop(self) -> None:
        for rt in reversed(list(self)):
            try:
                await rt.stop()
            except Exception:
                log.exception("module %s did not stop cleanly", rt.module.name)

    async def prepare_session(self, session: dict) -> None:
        for rt in self:
            await rt.prepare_session(session)

    async def end_session(self, sid: str) -> None:
        for rt in reversed(list(self)):
            try:
                await rt.end_session(sid)
            except Exception:
                log.exception("module %s session cleanup failed", rt.module.name)

    def toolkits(self, session=None) -> list[tuple[ToolGate, Any]]:
        out = []
        for rt in self:
            gate = rt.module.tools
            kit = (rt.session_toolkit(session) if gate.per_session and session is not None
                   else rt.toolkit()) if gate is not None else None
            if kit is not None:
                out.append((rt.module.tools, kit))
        return out

    def gate_for(self, kit) -> ToolGate | None:
        return next((rt.module.tools for rt in self if rt.module.tools and rt.owns_toolkit(kit)), None)

    def mutating_tools(self, session=None) -> frozenset[str]:
        static = frozenset(name for rt in self if rt.module.tools for name in rt.module.tools.mutating)
        dynamic = frozenset(name for _, kit in self.toolkits(session)
                            for name in getattr(kit, "mutating_tools", ()))
        return static | dynamic

    @property
    def gpu_taken(self) -> bool:
        return any(rt.gpu_taken for rt in self)

    def busy(self) -> bool:
        return any(rt.gpu_taken or rt.busy() for rt in self)

    def gpu_holders(self) -> list[str]:
        return [name for rt in self for name in rt.gpu_holders()]

    def gpu_hold(self) -> None:
        for rt in self:
            rt.gpu_hold()

    def gpu_resume(self, session_ids) -> None:
        for rt in self:
            rt.gpu_resume(session_ids)

    def features(self) -> dict:
        return {name: value for rt in self for name, value in rt.features().items()}

    async def app_root(self) -> dict:
        out: dict = {}
        for rt in self:
            out.update(await rt.app_root())
        return out

    def metrics(self, out, db) -> None:
        for rt in self:
            rt.metrics(out, db)


# route helpers for module handlers
def runtime(request, name: str) -> ModuleRuntime:
    """The named module's runtime on this daemon; 404 when it is absent (its routes are not installed then, so this
    only matters for a manager swapped under a running app in tests)."""
    host = getattr(request.app.state.manager, "modules", None)
    rt = host.get(name) if host is not None else None
    if rt is None:
        from .manager import HarnessError
        raise HarnessError(404, "Not Found")
    return rt


def manager(request):
    return request.app.state.manager
