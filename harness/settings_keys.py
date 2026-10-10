"""Explicit getter/setter specs for the v1 admin and app configuration registry."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .config import CORE_MODULE_NAMES, Config
from .settings import (
    APP_CAPABILITIES, APPLY_MODES, Bounds, EFFORTS, NOTIFY_COMPLETION, Registry, SettingSpec,
    module_installed, parse_value,
)

SMART_APPROVALS = "Smart approvals"
APP_DEFAULTS = "App defaults"
KEY_COMPACTION_SUMMARIZE_AT = "compaction.summarize_at"
KEY_COMPACTION_KEEP_RECENT = "compaction.keep_recent"
KEY_COMPACTION_RESET_AT = "compaction.reset_at"
KEY_COMPACTION_STATE_MAX_CHARS = "compaction.state_max_chars"
KEY_APP_DEFAULT_BACKEND = "app.default_backend"
KEY_APP_DEFAULT_MODEL = "app.default_model"
KEY_APP_DEFAULT_EFFORT = "app.default_effort"
KEY_APP_MAX_TURNS = "app.sessions.max_turns"
KEY_APP_MAX_COMPLETION_TOKENS = "app.sessions.max_completion_tokens"
KEY_APP_CAPABILITIES = "app.capabilities"
KEY_APP_NOTIFY_COMPLETION = "app.notify.completion"


def _require_url(value: str, label: str) -> list[str]:
    if not value.strip():
        return [f"{label} is not configured"]
    if not value.startswith(("http://", "https://")):
        return [f"{label} must be an http(s) URL"]
    return []


def _require_readable_file(path: str, label: str) -> list[str]:
    if not path.strip():
        return [f"{label} is not configured"]
    if not Path(path).is_file():
        return [f"{label} is missing"]
    return []


def check_web(cfg: Config) -> list[str]:
    errors = []
    if cfg.web.fixture_dir:
        if not Path(cfg.web.fixture_dir).is_dir():
            errors.append("web.fixture_dir is missing")
        return errors
    errors.extend(_require_url(cfg.web.searxng_url, "web.searxng_url"))
    return errors


def _set_module_enabled(cfg: Config, name: str, enabled: bool) -> None:
    """Switch ``cfg.<section>.enabled`` only. Never write ``cfg.modules`` or
    ``cfg.installed`` — those are installer/profile selection. Effective
    capability is ``module_effective(cfg, name)`` (installed AND enabled).
    """
    if name == "web":
        cfg.web.enabled = enabled


def _get_module_enabled(cfg: Config, name: str) -> bool:
    section = getattr(cfg, name)
    return bool(section.enabled)


def apply_cleanup_interval(manager, old, new) -> None:
    manager.maintenance.reschedule()


def _lt(a: float, b: float) -> bool:
    """a < b, spelled as a call so that a NaN operand fails the check (every NaN comparison is False)."""
    return a < b


def validate_compaction(cfg: Config, proposed: dict) -> list[dict]:
    elide = proposed.get("compaction.elide_at", cfg.elide_at)
    reset = proposed.get(KEY_COMPACTION_RESET_AT, cfg.reset_at)
    summarize = proposed.get(KEY_COMPACTION_SUMMARIZE_AT, cfg.summarize_at)
    keep = proposed.get(KEY_COMPACTION_KEEP_RECENT, cfg.keep_recent)
    errors = []
    if not _lt(elide, summarize):
        errors.append({"key": KEY_COMPACTION_SUMMARIZE_AT, "code": "cross_field",
                       "message": "compaction.summarize_at must be greater than compaction.elide_at"})
    if not _lt(elide, reset):
        errors.append({"key": KEY_COMPACTION_RESET_AT, "code": "cross_field",
                       "message": "compaction.reset_at must be greater than compaction.elide_at"})
    if not _lt(reset, summarize):
        errors.append({"key": KEY_COMPACTION_RESET_AT, "code": "cross_field",
                       "message": "compaction.reset_at must be less than compaction.summarize_at"})
    if not _lt(keep, summarize):
        errors.append({"key": KEY_COMPACTION_KEEP_RECENT, "code": "cross_field",
                       "message": "compaction.keep_recent must be less than compaction.summarize_at"})
    return errors


def enable_validator(specs: dict[str, SettingSpec]):
    """Refuse turning on a module that isn't installed for this profile or whose dependencies are missing."""
    checks = [(key, spec.modules[0], spec.enable_check) for key, spec in specs.items()
              if spec.enable_check is not None and spec.modules]

    def validate_enables(cfg: Config, proposed: dict) -> list[dict]:
        errors = []
        for key, module, check in checks:
            if proposed.get(key) is not True:
                continue
            if not module_installed(cfg, module):
                errors.append({"key": key, "code": "dependency",
                               "message": f"{module} is not installed for this profile"})
                continue
            for message in check(cfg):
                errors.append({"key": key, "code": "dependency", "message": message})
        return errors
    return validate_enables


def _int(key, label, help, category, default, getter, setter, minimum, maximum, yaml_path,
         apply_mode="live", modules=(), live_apply=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="int", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(minimum=minimum, maximum=maximum), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_apply,
    )


def _float(key, label, help, category, default, getter, setter, minimum, maximum, yaml_path,
           apply_mode="live", modules=(), live_apply=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="float", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(minimum=minimum, maximum=maximum), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_apply,
    )


def _bool(key, label, help, category, default, getter, setter, yaml_path, apply_mode="live",
          modules=(), enable_check=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="bool", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter, yaml_path=yaml_path,
        modules=modules, enable_check=enable_check,
    )


def _enum(key, label, help, category, default, getter, setter, values, yaml_path, modules=(),
          apply_mode="live", max_length=80, live_apply=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="enum", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(enum=values, max_length=max_length), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_apply,
    )


def _hidden(key, label, help, category, yaml_path, modules=()):
    def getter(cfg: Config):
        return None

    def setter(cfg: Config, value):
        raise ValueError("this setting is managed in local configuration")

    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="string", default=None,
        scope="admin", apply_mode="installer_only", getter=getter, setter=setter,
        sensitivity="hidden", readable=False, writable=False, yaml_path=yaml_path, modules=modules,
    )


def _get_max_turns(cfg: Config):
    return cfg.max_turns


def _set_max_turns(cfg: Config, value):
    cfg.max_turns = int(value)


def _cfg_attr(name: str, cast):
    """Getter and setter for a top-level Config field."""
    def getter(cfg: Config):
        return getattr(cfg, name)

    def setter(cfg: Config, value):
        setattr(cfg, name, cast(value))
    return getter, setter


def _get_max_tokens(cfg: Config):
    return cfg.max_completion_tokens


def _set_max_tokens(cfg: Config, value):
    cfg.max_completion_tokens = int(value)


def _get_elide(cfg: Config):
    return cfg.elide_at


def _set_elide(cfg: Config, value):
    cfg.elide_at = float(value)


def _get_summarize(cfg: Config):
    return cfg.summarize_at


def _set_summarize(cfg: Config, value):
    cfg.summarize_at = float(value)


def _get_keep(cfg: Config):
    return cfg.keep_recent


def _set_keep(cfg: Config, value):
    cfg.keep_recent = float(value)


def _get_reset_at(cfg: Config):
    return cfg.reset_at


def _set_reset_at(cfg: Config, value):
    cfg.reset_at = float(value)


def _get_state_max_chars(cfg: Config):
    return cfg.state_max_chars


def _set_state_max_chars(cfg: Config, value):
    cfg.state_max_chars = int(value)


def _get_idle(cfg: Config):
    return cfg.cleanup.container_idle_hours


def _set_idle(cfg: Config, value):
    cfg.cleanup.container_idle_hours = float(value)


def _get_retention(cfg: Config):
    return cfg.cleanup.workspace_retention_days


def _set_retention(cfg: Config, value):
    cfg.cleanup.workspace_retention_days = float(value)


def _get_quota(cfg: Config):
    return cfg.cleanup.workspace_quota_mb


def _set_quota(cfg: Config, value):
    cfg.cleanup.workspace_quota_mb = int(value)


def _get_min_free(cfg: Config):
    return cfg.cleanup.min_free_gb


def _set_min_free(cfg: Config, value):
    cfg.cleanup.min_free_gb = float(value)


def _get_interval(cfg: Config):
    return cfg.cleanup.interval_minutes


def _set_interval(cfg: Config, value):
    cfg.cleanup.interval_minutes = int(value)


def _get_page_chars(cfg: Config):
    return cfg.web.page_chars


def _set_page_chars(cfg: Config, value):
    cfg.web.page_chars = int(value)


def _get_max_bytes(cfg: Config):
    return cfg.web.max_bytes


def _set_max_bytes(cfg: Config, value):
    cfg.web.max_bytes = int(value)


def _get_doc_bytes(cfg: Config):
    return cfg.web.max_document_bytes


def _set_doc_bytes(cfg: Config, value):
    cfg.web.max_document_bytes = int(value)


def _get_web_timeout(cfg: Config):
    return cfg.web.timeout_seconds


def _set_web_timeout(cfg: Config, value):
    cfg.web.timeout_seconds = float(value)


def _get_quote(cfg: Config):
    return cfg.web.quote_check


def _set_quote(cfg: Config, value):
    cfg.web.quote_check = bool(value)


def _get_smart_enabled(cfg: Config):
    return bool(cfg.smart_approvals.enabled)


def _set_smart_enabled(cfg: Config, value):
    cfg.smart_approvals.enabled = bool(value)


def _get_smart_mode(cfg: Config):
    return cfg.smart_approvals.mode


def _set_smart_mode(cfg: Config, value):
    from .smart_approvals import MODES
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    cfg.smart_approvals.mode = mode


def apply_smart_mode(manager, old, new) -> None:
    """Last writer: Settings `smart_approvals.mode` writes the same overlay as PUT."""
    from .smart_approvals import save_runtime_mode
    db = getattr(manager, "db", None)
    if db is None:
        return
    save_runtime_mode(db, str(new).strip().lower())


def _get_smart_provider(cfg: Config):
    return cfg.smart_approvals.provider


def _set_smart_provider(cfg: Config, value):
    from .smart_approvals import PROVIDERS
    provider = str(value).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    cfg.smart_approvals.provider = provider


def _get_smart_model(cfg: Config):
    return cfg.smart_approvals.model


def _set_smart_model(cfg: Config, value):
    model = str(value).strip()[:80]
    if not model:
        raise ValueError("model is empty")
    cfg.smart_approvals.model = model


def _get_smart_timeout(cfg: Config):
    return cfg.smart_approvals.timeout_seconds


def _set_smart_timeout(cfg: Config, value):
    timeout = float(value)
    if timeout <= 0 or timeout > 60:
        raise ValueError("smart_approvals.timeout_seconds must be between 0 and 60")
    cfg.smart_approvals.timeout_seconds = timeout


def _get_smart_confidence(cfg: Config):
    return cfg.smart_approvals.min_confidence


def _set_smart_confidence(cfg: Config, value):
    confidence = float(value)
    if not 0 <= confidence <= 1:
        raise ValueError("smart_approvals.min_confidence must be between 0 and 1")
    cfg.smart_approvals.min_confidence = confidence


def _get_local_model(cfg: Config):
    return cfg.default_model


def _set_local_model(cfg: Config, value):
    model = str(value).strip()
    if model not in cfg.models:
        raise ValueError(f"unknown model {model!r}; known: {', '.join(cfg.models) or '(none)'}")
    cfg.default_model = model


def _backend_model_get(name: str):
    def getter(cfg: Config):
        backend = cfg.backends.get(name)
        return backend.model if backend is not None else ""
    return getter


def _backend_model_set(name: str):
    def setter(cfg: Config, value):
        backend = cfg.backends.get(name)
        if backend is None:
            raise ValueError(f"backend {name!r} is not configured")
        model = str(value).strip()
        if not model:
            raise ValueError("model is empty")
        if len(model) > 80:
            raise ValueError("model is too long")
        backend.model = model
    return setter


def _backend_effort_get(name: str):
    def getter(cfg: Config):
        backend = cfg.backends.get(name)
        return backend.effort if backend is not None else ""
    return getter


def _backend_effort_set(name: str):
    def setter(cfg: Config, value):
        backend = cfg.backends.get(name)
        if backend is None:
            raise ValueError(f"backend {name!r} is not configured")
        effort = str(value).strip()
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
        backend.effort = effort
    return setter


def _enable_get(name: str):
    def getter(cfg: Config):
        return _get_module_enabled(cfg, name)
    return getter


def _enable_set(name: str):
    def setter(cfg: Config, value):
        _set_module_enabled(cfg, name, bool(value))
    return setter


def _app_get(key: str, default):
    def getter(cfg: Config):
        values = getattr(cfg, "_app_values", {}) or {}
        return values.get(key, default)
    return getter


def _app_set(key: str):
    def setter(cfg: Config, value):
        values = dict(getattr(cfg, "_app_values", {}) or {})
        values[key] = value
        cfg._app_values = values
    return setter


STATIC_ADMIN: list[SettingSpec] = [
    _int("sessions.max_turns", "Maximum turns",
         "Per-run turn cap for new sessions. Changing this does not raise an active run's budget.",
         "Sessions", 80, _get_max_turns, _set_max_turns, 1, 500, ("budgets", "max_turns")),
    _int("sessions.max_completion_tokens", "Maximum completion tokens",
         "Per-run completion-token cap for new sessions. Changing this does not raise an active run's budget.",
         "Sessions", 200000, _get_max_tokens, _set_max_tokens, 1000, 2_000_000,
         ("budgets", "max_completion_tokens")),
    _float("sessions.approval_timeout_seconds", "Approval deadline (seconds)",
           "Deny a pending approval nobody decided after this long and end its run. 0 never expires one.",
           "Sessions", 86400, *_cfg_attr("approval_timeout_seconds", float), 0, 30 * 86400,
           ("budgets", "approval_timeout_seconds")),
    _float("sessions.max_run_seconds", "Member and App run time (seconds)",
           "End a member's or an App's run after this long running (approval, queue and reply waits do not count). "
           "0 is no limit. The owner's own runs have none.",
           "Sessions", 3600, *_cfg_attr("max_run_seconds", float), 0, 7 * 86400, ("budgets", "max_run_seconds")),
    _int("sessions.app_max_running", "App running sessions",
         "How many sessions an App may have running or parked at once, unless the owner set its own cap.",
         "Sessions", 2, *_cfg_attr("app_max_running", int), 1, 100, ("budgets", "app_max_running")),
    _int("sessions.app_max_queued", "App queued sessions",
         "How many sessions an App may have queued or parked before new ones are refused (429), unless the owner "
         "set its own cap.",
         "Sessions", 4, *_cfg_attr("app_max_queued", int), 1, 1000, ("budgets", "app_max_queued")),
    _float("compaction.elide_at", "Elide at",
           "Fraction of context at which old tool outputs are shortened.",
           "Compaction", 0.55, _get_elide, _set_elide, 0.10, 0.90, ("compaction", "elide_at")),
    _float(KEY_COMPACTION_RESET_AT, "Reset at",
           "Fraction of context at which a round reset fires when valid state is saved. Must be greater than "
           "elide_at and less than summarize_at.",
           "Compaction", 0.60, _get_reset_at, _set_reset_at, 0.15, 0.95, ("compaction", "reset_at")),
    _float(KEY_COMPACTION_SUMMARIZE_AT, "Summarize at",
           "Fraction of context at which older turns are summarized. Must be greater than elide_at.",
           "Compaction", 0.65, _get_summarize, _set_summarize, 0.15, 0.95, ("compaction", "summarize_at")),
    _float(KEY_COMPACTION_KEEP_RECENT, "Keep recent",
           "Fraction of context kept verbatim after a summary. Must be less than summarize_at.",
           "Compaction", 0.20, _get_keep, _set_keep, 0.05, 0.50, ("compaction", "keep_recent")),
    _int(KEY_COMPACTION_STATE_MAX_CHARS, "State max characters",
         "Maximum characters of the serialized update_state object. Saved state is re-injected on a round reset.",
         "Compaction", 8000, _get_state_max_chars, _set_state_max_chars, 256, 100_000,
         ("compaction", "state_max_chars")),
    _float("cleanup.container_idle_hours", "Container idle hours",
           "Remove a finished session's stopped container after this many hours.",
           "Cleanup", 24, _get_idle, _set_idle, 0.25, 168, ("cleanup", "container_idle_hours")),
    _float("cleanup.workspace_retention_days", "Workspace retention days",
           "Delete a finished session's workspace after this many days.",
           "Cleanup", 14, _get_retention, _set_retention, 1, 365, ("cleanup", "workspace_retention_days")),
    _int("cleanup.workspace_quota_mb", "Workspace quota (MB)",
         "Per-session workspace limit unless a project overrides it.",
         "Cleanup", 5000, _get_quota, _set_quota, 50, 100_000, ("cleanup", "workspace_quota_mb")),
    _float("cleanup.min_free_gb", "Minimum free disk (GB)",
           "Refuse new sessions when the data drive has less free space than this.",
           "Cleanup", 20, _get_min_free, _set_min_free, 1, 1000, ("cleanup", "min_free_gb")),
    _int("cleanup.interval_minutes", "Cleanup interval (minutes)",
         "How often idle containers and old workspaces are swept. Applied live by rescheduling the cleanup task.",
         "Cleanup", 60, _get_interval, _set_interval, 5, 1440, ("cleanup", "interval_minutes"),
         live_apply=apply_cleanup_interval),
    _int("web.page_chars", "Page character limit",
         "Characters returned per web_fetch page.",
         "Web", 15000, _get_page_chars, _set_page_chars, 1000, 200_000, ("web", "page_chars"),
         modules=("web",)),
    _int("web.max_bytes", "Download byte limit",
         "Refuse larger ordinary page downloads.",
         "Web", 5 * 2**20, _get_max_bytes, _set_max_bytes, 64 * 1024, 50 * 2**20, ("web", "max_bytes"),
         modules=("web",)),
    _int("web.max_document_bytes", "Document byte limit",
         "Refuse larger PDF or Word downloads.",
         "Web", 25 * 2**20, _get_doc_bytes, _set_doc_bytes, 2**20, 100 * 2**20, ("web", "max_document_bytes"),
         modules=("web",)),
    _float("web.timeout_seconds", "Web timeout (seconds)",
           "Network timeout for search and fetch.",
           "Web", 20, _get_web_timeout, _set_web_timeout, 5, 120, ("web", "timeout_seconds"),
           modules=("web",)),
    _bool("web.quote_check", "Quote checking",
          "Require quoted passages in final answers to appear in something the agent read.",
          "Web", True, _get_quote, _set_quote, ("web", "quote_check"), modules=("web",)),
    _bool("smart_approvals.enabled", SMART_APPROVALS,
          "Runtime enable for the hosted smart-approval reviewer. Does not configure a secret_ref.",
          SMART_APPROVALS, False, _get_smart_enabled, _set_smart_enabled,
          ("smart_approvals", "enabled")),
    _enum("smart_approvals.mode", "Smart-approval mode",
          "off, shadow, or auto. Last writer among this setting and PUT /smart-approvals "
          "wins; off means no reviewer calls.",
          SMART_APPROVALS, "shadow", _get_smart_mode, _set_smart_mode,
          ("off", "shadow", "auto"), ("smart_approvals", "mode"),
          live_apply=apply_smart_mode),
    _enum("smart_approvals.provider", "Smart-approval provider",
          "Hosted reviewer provider. openai or anthropic.",
          SMART_APPROVALS, "openai", _get_smart_provider, _set_smart_provider,
          ("openai", "anthropic"), ("smart_approvals", "provider")),
    SettingSpec(
        key="smart_approvals.model", label="Smart-approval model",
        help="Hosted reviewer model id. Existing in-flight reviews keep the model they started with.",
        category=SMART_APPROVALS, value_type="string", default="gpt-4.1-mini", scope="admin",
        apply_mode="live", getter=_get_smart_model, setter=_set_smart_model,
        bounds=Bounds(min_length=1, max_length=80), yaml_path=("smart_approvals", "model"),
    ),
    _float("smart_approvals.timeout_seconds", "Smart-approval timeout (seconds)",
           "Give up on a hung reviewer request after this long.",
           SMART_APPROVALS, 8, _get_smart_timeout, _set_smart_timeout, 0.1, 60,
           ("smart_approvals", "timeout_seconds")),
    _float("smart_approvals.min_confidence", "Smart-approval min confidence",
           "Hosted reviewer must meet this confidence before auto mode may approve.",
           SMART_APPROVALS, 0.85, _get_smart_confidence, _set_smart_confidence, 0, 1,
           ("smart_approvals", "min_confidence")),
    _bool("web.enabled", "Web tools",
          "Runtime enable for web_search / web_fetch. Does not install the web module.",
          "Features", False, _enable_get("web"), _enable_set("web"), ("web", "enabled"),
          apply_mode="daemon_restart", modules=("web",), enable_check=check_web),

    _hidden("listen.host", "Listen address", "Bind address for the daemon HTTP server.", "Network",
            ("listen", "host")),
    _hidden("listen.port", "Listen port", "TCP port for the daemon HTTP server.", "Network",
            ("listen", "port")),
    _hidden("paths.data_dir", "Data directory", "SQLite database, workspaces, and transcripts.", "Paths",
            ("data_dir",)),
    _hidden("paths.repos_dir", "Repository directory", "Host-side clones for local:<name> git_clone.", "Paths",
            ("repos_dir",)),
    _hidden("smart_approvals.secret_ref", "Smart-approval secret",
            "Opaque name of the hosted reviewer key file. Managed in local configuration.",
            SMART_APPROVALS, ("smart_approvals", "secret_ref")),
    _hidden("smart_approvals.proxy", "Smart-approval proxy",
            "Optional explicit proxy for the hosted reviewer. Managed in local configuration.",
            SMART_APPROVALS, ("smart_approvals", "proxy")),
    _hidden("install.profile", "Install profile", "full or service. Chosen by the installer.", "Install",
            ("profile",)),
]

def module_switch_spec(name: str) -> SettingSpec:
    return _hidden(
        f"modules.{name}", f"Module: {name}",
        f"{name} installation/profile selection. Change this with the installer, not this registry.",
        "Install", ("modules", name),
    )


for _mod in CORE_MODULE_NAMES:
    STATIC_ADMIN.append(module_switch_spec(_mod))


APP_SPECS: list[SettingSpec] = [
    SettingSpec(
        key=KEY_APP_DEFAULT_BACKEND, label="Default backend",
        help="Used only when a session request omits backend. Cannot select an unassigned provider.",
        category=APP_DEFAULTS, value_type="string", default="", scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_DEFAULT_BACKEND, ""), setter=_app_set(KEY_APP_DEFAULT_BACKEND),
        bounds=Bounds(max_length=40), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_DEFAULT_MODEL, label="Default model",
        help="Used only when a session request omits model.",
        category=APP_DEFAULTS, value_type="string", default="", scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_DEFAULT_MODEL, ""), setter=_app_set(KEY_APP_DEFAULT_MODEL),
        bounds=Bounds(max_length=80), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_DEFAULT_EFFORT, label="Default effort",
        help="Used only when a hosted-backend session request omits effort.",
        category=APP_DEFAULTS, value_type="enum", default="", scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_DEFAULT_EFFORT, ""), setter=_app_set(KEY_APP_DEFAULT_EFFORT),
        bounds=Bounds(enum=("",) + EFFORTS), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_MAX_TURNS, label="Maximum turns",
        help="Per-session turn cap, never higher than the owner limit.",
        category="App budgets", value_type="int", default=None, scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_MAX_TURNS, None), setter=_app_set(KEY_APP_MAX_TURNS),
        bounds=Bounds(minimum=1, maximum=500), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_MAX_COMPLETION_TOKENS, label="Maximum completion tokens",
        help="Per-session completion-token cap, never higher than the owner limit.",
        category="App budgets", value_type="int", default=None, scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_MAX_COMPLETION_TOKENS, None),
        setter=_app_set(KEY_APP_MAX_COMPLETION_TOKENS),
        bounds=Bounds(minimum=1000, maximum=2_000_000), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_CAPABILITIES, label="Enabled capabilities",
        help="Subset of capabilities already granted by the token, installed by the daemon, and allowed by policy.",
        category="App capabilities", value_type="string_list", default=None, scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_CAPABILITIES, None), setter=_app_set(KEY_APP_CAPABILITIES),
        bounds=Bounds(enum=APP_CAPABILITIES), capabilities=("sessions",),
    ),
    SettingSpec(
        key=KEY_APP_NOTIFY_COMPLETION, label="Completion notifications",
        help="inherit uses the owner channel; never silences this app's session-completion notifications.",
        category="App notifications", value_type="enum", default="inherit", scope="app", apply_mode="live",
        getter=_app_get(KEY_APP_NOTIFY_COMPLETION, "inherit"), setter=_app_set(KEY_APP_NOTIFY_COMPLETION),
        bounds=Bounds(enum=NOTIFY_COMPLETION), capabilities=("sessions",),
    ),
]


def backend_specs(cfg: Config) -> list[SettingSpec]:
    specs = []
    if module_installed(cfg, "local_model") and cfg.models:
        specs.append(SettingSpec(
            key="backends.local.model", label="Local default model",
            help="Default local model for new sessions. Existing sessions keep the model they started with.",
            category="Backends", value_type="enum", default=next(iter(cfg.models), ""),
            scope="admin", apply_mode="live", getter=_get_local_model, setter=_set_local_model,
            bounds=Bounds(enum=tuple(cfg.models), max_length=80),
            yaml_path=("default_model",), modules=("local_model",),
        ))
    for name, backend in cfg.backends.items():
        specs.append(SettingSpec(
            key=f"backends.{name}.model", label=f"{name} default model",
            help=f"Default model for new {name} sessions. Existing sessions keep the model they started with.",
            category="Backends", value_type="string", default=backend.model, scope="admin", apply_mode="live",
            getter=_backend_model_get(name), setter=_backend_model_set(name),
            bounds=Bounds(min_length=1, max_length=80), yaml_path=("backends", name, "model"),
        ))
        specs.append(SettingSpec(
            key=f"backends.{name}.effort", label=f"{name} default effort",
            help=f"Default effort for new {name} sessions. Existing sessions keep the effort they started with.",
            category="Backends", value_type="enum", default=backend.effort, scope="admin", apply_mode="live",
            getter=_backend_effort_get(name), setter=_backend_effort_set(name),
            bounds=Bounds(enum=EFFORTS), yaml_path=("backends", name, "effort"),
        ))
    return specs


def module_specs(cfg: Config) -> list[SettingSpec]:
    """The present add-on modules' keys (harness/modules.py): absent modules have none."""
    from . import modules
    return [*modules.settings_specs(cfg), *(module_switch_spec(name) for name in modules.present_switches(cfg))]


def _app_specs(cfg: Config) -> list[SettingSpec]:
    from . import modules
    extra = tuple(name for name in modules.app_capabilities(cfg) if name not in APP_CAPABILITIES)
    return [replace(spec, bounds=replace(spec.bounds, enum=(*spec.bounds.enum, *extra)))
            if spec.key == KEY_APP_CAPABILITIES and extra else spec for spec in APP_SPECS]


def build_registry(cfg: Config) -> Registry:
    specs = {spec.key: spec for spec in [*STATIC_ADMIN, *module_specs(cfg),
                                         *backend_specs(cfg), *_app_specs(cfg)]}
    from .modules import present
    addons = present(cfg)
    return Registry(specs=specs, validators=[validate_compaction, enable_validator(specs),
                    *(v for module in addons for v in module.settings_validators)],
                    change_validators=[v for module in addons for v in module.settings_change_validators])


def assert_explicit_registry(registry: Registry) -> None:
    """Coverage helper: every spec has an explicit getter/setter and metadata, no generic apply modes."""
    for spec in registry.specs.values():
        assert spec.key and "." in spec.key
        assert spec.label and spec.help and spec.category
        assert spec.value_type in ("bool", "int", "float", "string", "enum", "string_list", "discovery_root_list")
        assert spec.scope in ("app", "admin")
        assert spec.apply_mode in APPLY_MODES
        assert spec.sensitivity in ("public", "redact", "hidden")
        assert callable(spec.getter) and callable(spec.setter)
        assert spec.getter.__name__ != "<lambda>" or spec.key.startswith("backends.")
        assert spec.setter.__name__ != "<lambda>" or spec.key.startswith("backends.")
        name = spec.getter.__name__
        assert "getattr" not in name
