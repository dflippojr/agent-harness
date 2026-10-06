"""Explicit getter/setter specs for the v1 admin and app configuration registry."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .config import CORE_MODULE_NAMES, Config
from .settings import (
    APP_CAPABILITIES, APPLY_MODES, Bounds, EFFORTS, NOTIFY_COMPLETION, Registry, SettingSpec,
    module_installed, parse_value,
)

GPU_GUARD = "GPU guard"
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

TIME_OF_DAY = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


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


def check_search(cfg: Config) -> list[str]:
    return []


def check_jobs(cfg: Config) -> list[str]:
    return []


def check_endpoint(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("endpoint requires the local_model module")
    if not cfg.models:
        errors.append("endpoint requires a configured local model")
    return errors


def check_gpu_guard(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("gpu_guard requires the local_model module")
    if not cfg.gpu_guard.pause_flag.strip():
        errors.append("gpu_guard.pause_flag is not configured")
    return errors


def check_backup(cfg: Config) -> list[str]:
    if not cfg.backup.dir.strip():
        return ["backup.dir is not configured"]
    parent = Path(cfg.backup.dir).expanduser()
    if not parent.parent.exists():
        return ["backup.dir parent is missing"]
    return []


def check_skills(cfg: Config) -> list[str]:
    return []


def _set_module_enabled(cfg: Config, name: str, enabled: bool) -> None:
    """Switch ``cfg.<section>.enabled`` only. Never write ``cfg.modules`` or
    ``cfg.installed`` — those are installer/profile selection. Effective
    capability is ``module_effective(cfg, name)`` (installed AND enabled).
    """
    if name == "web":
        cfg.web.enabled = enabled
    elif name == "search":
        cfg.search.enabled = enabled
    elif name == "jobs":
        cfg.jobs.enabled = enabled
    elif name == "endpoint":
        cfg.endpoint.enabled = enabled
    elif name == "gpu_guard":
        cfg.gpu_guard.enabled = enabled
    elif name == "backup":
        cfg.backup.enabled = enabled
    elif name == "skills":
        cfg.skills.enabled = enabled


def _get_module_enabled(cfg: Config, name: str) -> bool:
    section = getattr(cfg, name)
    return bool(section.enabled)


def apply_cleanup_interval(manager, old, new) -> None:
    manager.maintenance.reschedule()


def apply_endpoint_queue(manager, old, new) -> None:
    manager.runner.gate.max_waiting = manager.cfg.endpoint.max_waiting
    manager.runner.gate.fair_seconds = manager.cfg.endpoint.agent_fair_seconds


def apply_jobs_poll(manager, old, new) -> None:
    if manager.jobs is not None:
        manager.jobs.poll_seconds = manager.cfg.jobs.poll_seconds


def apply_backup_schedule(manager, old, new) -> None:
    manager.maintenance.reschedule_backup()


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


def _get_max_waiting(cfg: Config):
    return cfg.endpoint.max_waiting


def _set_max_waiting(cfg: Config, value):
    cfg.endpoint.max_waiting = int(value)


def _get_fair(cfg: Config):
    return cfg.endpoint.agent_fair_seconds


def _set_fair(cfg: Config, value):
    cfg.endpoint.agent_fair_seconds = float(value)


def _get_req_timeout(cfg: Config):
    return cfg.endpoint.request_timeout_seconds


def _set_req_timeout(cfg: Config, value):
    cfg.endpoint.request_timeout_seconds = float(value)


def _get_gpu_poll(cfg: Config):
    return cfg.gpu_guard.poll_seconds


def _set_gpu_poll(cfg: Config, value):
    cfg.gpu_guard.poll_seconds = float(value)


def _get_gpu_resume(cfg: Config):
    return cfg.gpu_guard.resume_after_seconds


def _set_gpu_resume(cfg: Config, value):
    cfg.gpu_guard.resume_after_seconds = float(value)


def _get_gpu_drain(cfg: Config):
    return cfg.gpu_guard.drain_timeout_seconds


def _set_gpu_drain(cfg: Config, value):
    cfg.gpu_guard.drain_timeout_seconds = float(value)


def _get_jobs_poll(cfg: Config):
    return cfg.jobs.poll_seconds


def _set_jobs_poll(cfg: Config, value):
    cfg.jobs.poll_seconds = float(value)


def _get_backup_at(cfg: Config):
    return cfg.backup.at


def _set_backup_at(cfg: Config, value):
    text = str(value)
    if not TIME_OF_DAY.fullmatch(text):
        raise ValueError("backup.at must be HH:MM in 24-hour local time")
    cfg.backup.at = text


def _get_backup_keep(cfg: Config):
    return cfg.backup.keep_days


def _set_backup_keep(cfg: Config, value):
    cfg.backup.keep_days = int(value)


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
    _int("endpoint.max_waiting", "Endpoint queue depth",
         "Inference requests waiting for the GPU before new ones get 429.",
         "Endpoint", 4, _get_max_waiting, _set_max_waiting, 0, 32, ("endpoint", "max_waiting"),
         modules=("endpoint",), live_apply=apply_endpoint_queue),
    _float("endpoint.agent_fair_seconds", "Agent fairness (seconds)",
           "After an agent turn waits this long, new endpoint requests queue behind it.",
           "Endpoint", 90, _get_fair, _set_fair, 10, 600, ("endpoint", "agent_fair_seconds"),
           modules=("endpoint",), live_apply=apply_endpoint_queue),
    _float("endpoint.request_timeout_seconds", "Endpoint request timeout (seconds)",
           "Give up on a hung inference request after this long.",
           "Endpoint", 1800, _get_req_timeout, _set_req_timeout, 30, 7200,
           ("endpoint", "request_timeout_seconds"), modules=("endpoint",)),
    _float("gpu_guard.poll_seconds", "GPU guard poll (seconds)",
           "How often the GPU guard looks for games or Plex transcodes.",
           GPU_GUARD, 10, _get_gpu_poll, _set_gpu_poll, 2, 60, ("gpu_guard", "poll_seconds"),
           modules=("gpu_guard",)),
    _float("gpu_guard.resume_after_seconds", "GPU resume delay (seconds)",
           "The GPU must stay clear this long before the model is reloaded.",
           GPU_GUARD, 180, _get_gpu_resume, _set_gpu_resume, 10, 1800,
           ("gpu_guard", "resume_after_seconds"), modules=("gpu_guard",)),
    _float("gpu_guard.drain_timeout_seconds", "GPU drain timeout (seconds)",
           "Longest wait for the current model turn before the server is stopped.",
           GPU_GUARD, 300, _get_gpu_drain, _set_gpu_drain, 30, 1800,
           ("gpu_guard", "drain_timeout_seconds"), modules=("gpu_guard",)),
    _float("jobs.poll_seconds", "Job polling interval (seconds)",
           "How often scheduled jobs are checked.",
           "Jobs", 30, _get_jobs_poll, _set_jobs_poll, 5, 300, ("jobs", "poll_seconds"),
           modules=("jobs",), live_apply=apply_jobs_poll),
    SettingSpec(
        key="backup.at", label="Backup time", help="Local time (HH:MM) for the nightly backup.",
        category="Backup", value_type="string", default="03:30", scope="admin", apply_mode="live",
        getter=_get_backup_at, setter=_set_backup_at, bounds=Bounds(pattern=TIME_OF_DAY.pattern),
        yaml_path=("backup", "at"), modules=("backup",),
        live_apply=apply_backup_schedule, live_undo=apply_backup_schedule,
    ),
    _int("backup.keep_days", "Backup retention (days)",
         "Delete dated backup folders older than this.",
         "Backup", 14, _get_backup_keep, _set_backup_keep, 1, 365, ("backup", "keep_days"),
         modules=("backup",)),
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
    _bool("search.enabled", "Session search",
          "Runtime enable for session search. Does not install the search module.",
          "Features", False, _enable_get("search"), _enable_set("search"), ("search", "enabled"),
          apply_mode="daemon_restart", modules=("search",), enable_check=check_search),
    _bool("jobs.enabled", "Scheduled jobs",
          "Runtime enable for scheduled jobs. Does not install the jobs module.",
          "Features", False, _enable_get("jobs"), _enable_set("jobs"), ("jobs", "enabled"),
          apply_mode="daemon_restart", modules=("jobs",), enable_check=check_jobs),
    _bool("endpoint.enabled", "Inference endpoint",
          "Runtime enable for the OpenAI/Anthropic-compatible endpoint.",
          "Features", False, _enable_get("endpoint"), _enable_set("endpoint"), ("endpoint", "enabled"),
          apply_mode="daemon_restart", modules=("endpoint",), enable_check=check_endpoint),
    _bool("gpu_guard.enabled", GPU_GUARD,
          "Runtime enable for pausing the model while a game or Plex transcode needs the GPU.",
          "Features", False, _enable_get("gpu_guard"), _enable_set("gpu_guard"), ("gpu_guard", "enabled"),
          apply_mode="daemon_restart", modules=("gpu_guard",), enable_check=check_gpu_guard),
    _bool("backup.enabled", "Backups",
          "Runtime enable for the nightly backup. Does not change the backup directory.",
          "Features", False, _enable_get("backup"), _enable_set("backup"), ("backup", "enabled"),
          apply_mode="daemon_restart", modules=("backup",), enable_check=check_backup),
    _bool("skills.enabled", "Instruction skills",
          "Runtime enable for owner-approved instruction skills. Does not install the skills module.",
          "Features", False, _enable_get("skills"), _enable_set("skills"), ("skills", "enabled"),
          apply_mode="daemon_restart", modules=("skills",), enable_check=check_skills),
    _hidden("listen.host", "Listen address", "Bind address for the daemon HTTP server.", "Network",
            ("listen", "host")),
    _hidden("listen.port", "Listen port", "TCP port for the daemon HTTP server.", "Network",
            ("listen", "port")),
    _hidden("paths.data_dir", "Data directory", "SQLite database, workspaces, and transcripts.", "Paths",
            ("data_dir",)),
    _hidden("paths.repos_dir", "Repository directory", "Host-side clones for local:<name> git_clone.", "Paths",
            ("repos_dir",)),
    _hidden("backup.dir", "Backup directory", "Where nightly backups are written.", "Backup",
            ("backup", "dir"), modules=("backup",)),
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


def _get_discovery_enabled(cfg):
    return cfg.remote_control.discovery.enabled


def _set_discovery_enabled(cfg, value):
    cfg.remote_control.discovery.enabled = value


def _get_discovery_roots(cfg):
    return list(cfg.remote_control.discovery.roots)


def _set_discovery_roots(cfg, value):
    # Lexical only: this setter re-runs for every settings PATCH and at startup, so it must not
    # depend on the filesystem. Live checks run in validate_discovery_roots_change (changes only)
    # and again at scan start.
    from .discovery_paths import DiscoveryError, lexical
    if not isinstance(value, list) or len(value) > 8 or not all(isinstance(v, str) for v in value):
        raise DiscoveryError('up_to_eight_roots')
    cfg.remote_control.discovery.roots = [lexical(v) for v in value]


def _get_discovery_depth(cfg):
    return cfg.remote_control.discovery.max_depth


def _set_discovery_depth(cfg, value):
    cfg.remote_control.discovery.max_depth = value


def validate_discovery(cfg, proposed):
    """Cheap cross-field rule; runs on the full merged overlay, so no filesystem access."""
    if cfg.remote_control.discovery.enabled and not cfg.remote_control.discovery.roots:
        return [{'key': 'remote_control.discovery.roots', 'code': 'valid_root_required',
                 'message': 'valid_root_required'}]
    return []


def validate_discovery_roots_change(cfg, proposed):
    """Live Windows validation and canonicalization, only when this request changes discovery."""
    from .discovery_paths import DiscoveryError, WindowsDirectories
    if not any(k.startswith('remote_control.discovery.') for k in proposed):
        return []
    try:
        roots = WindowsDirectories(cfg).roots(cfg.remote_control.discovery.roots)
        if cfg.remote_control.discovery.enabled and not roots:
            raise DiscoveryError('valid_root_required')
    except DiscoveryError as error:
        return [{'key': 'remote_control.discovery.roots', 'code': error.code, 'message': error.code}]
    cfg.remote_control.discovery.roots = [i.path for i in roots]
    return []


def discovery_specs():
    help_text = ('Windows owner-only, default-off metadata discovery. No file contents, trust or launch. '
                 'Limits: 20,000 directories; 500 candidates; 30 seconds; 50 errors; '
                 'one active scan; results expire after 15 minutes. Hidden/system entries, '
                 'all reparse points (including OneDrive), credentials, caches and build folders are excluded.')
    specs = [
        _bool('remote_control.discovery.enabled', 'Folder discovery', help_text,
              'Remote Control discovery', False, _get_discovery_enabled, _set_discovery_enabled,
              ('remote_control', 'discovery', 'enabled')),
        SettingSpec(key='remote_control.discovery.roots', label='Discovery roots', help=help_text,
                    category='Remote Control discovery', value_type='discovery_root_list', default=[],
                    scope='admin', apply_mode='live', getter=_get_discovery_roots, setter=_set_discovery_roots,
                    bounds=Bounds(max_length=8), yaml_path=('remote_control', 'discovery', 'roots')),
        _int('remote_control.discovery.max_depth', 'Discovery depth', help_text,
             'Remote Control discovery', 3, _get_discovery_depth, _set_discovery_depth, 1, 5,
             ('remote_control', 'discovery', 'max_depth')),
    ]
    for spec in specs:
        spec.platforms = ('win32',)
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
    specs = {spec.key: spec for spec in [*STATIC_ADMIN, *module_specs(cfg), *discovery_specs(),
                                         *backend_specs(cfg), *_app_specs(cfg)]}
    return Registry(specs=specs, validators=[validate_compaction, enable_validator(specs), validate_discovery],
                    change_validators=[validate_discovery_roots_change])


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
