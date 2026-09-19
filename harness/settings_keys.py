"""Explicit getter/setter specs for the v1 admin and app configuration registry."""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config, MODULE_NAMES
from .settings import (
    APP_CAPABILITIES, APPLY_MODES, Bounds, EFFORTS, NOTIFY_COMPLETION, Registry, SettingSpec,
    module_installed, parse_value,
)

TIME_OF_DAY = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _require_url(value: str, label: str) -> list[str]:
    if not value.strip():
        return [f"{label} is not configured"]
    if not (value.startswith("http://") or value.startswith("https://")):
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


def check_images(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("images requires the local_model module")
    if not cfg.images.comfy_dir or not Path(cfg.images.comfy_dir).exists():
        errors.append("images.comfy_dir is missing")
    return errors


def check_image_edit(cfg: Config) -> list[str]:
    errors = check_images(cfg)
    if not module_installed(cfg, "image_edit"):
        errors.append("image_edit is not installed for this profile")
    from . import image_edit
    status = image_edit.assets_status(cfg.images)
    if status["missing"] or status["hash_ok"] is False:
        errors.append(status["setup"])
    return errors


def check_gpu_guard(cfg: Config) -> list[str]:
    errors = []
    if not module_installed(cfg, "local_model"):
        errors.append("gpu_guard requires the local_model module")
    if not cfg.gpu_guard.pause_flag.strip():
        errors.append("gpu_guard.pause_flag is not configured")
    return errors


def check_notifications(cfg: Config) -> list[str]:
    errors = _require_url(cfg.notify.server, "notify.server")
    if not cfg.notify.topic.strip():
        errors.append("notify.topic is not configured")
    if cfg.notify.token_file:
        errors.extend(_require_readable_file(cfg.notify.token_file, "notify.token_file"))
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


ENABLE_CHECKS = {
    "web.enabled": check_web,
    "search.enabled": check_search,
    "jobs.enabled": check_jobs,
    "endpoint.enabled": check_endpoint,
    "images.enabled": check_images,
    "images.edit_enabled": check_image_edit,
    "gpu_guard.enabled": check_gpu_guard,
    "notifications.enabled": check_notifications,
    "backup.enabled": check_backup,
    "skills.enabled": check_skills,
}


def _set_module_enabled(cfg: Config, name: str, enabled: bool) -> None:
    """Switch ``cfg.<section>.enabled`` only. Never write ``cfg.modules`` or
    ``cfg.installed`` — those are installer/profile selection. Effective
    capability is ``module_effective(cfg, name)`` (installed AND enabled).
    """
    if name == "notifications":
        cfg.notify.enabled = enabled
    elif name == "web":
        cfg.web.enabled = enabled
    elif name == "search":
        cfg.search.enabled = enabled
    elif name == "jobs":
        cfg.jobs.enabled = enabled
    elif name == "endpoint":
        cfg.endpoint.enabled = enabled
    elif name == "images":
        cfg.images.enabled = enabled
    elif name == "image_edit":
        cfg.images.edit_enabled = enabled
    elif name == "gpu_guard":
        cfg.gpu_guard.enabled = enabled
    elif name == "backup":
        cfg.backup.enabled = enabled
    elif name == "skills":
        cfg.skills.enabled = enabled


def _get_module_enabled(cfg: Config, name: str) -> bool:
    if name == "notifications":
        return cfg.notify.enabled
    if name == "image_edit":
        return cfg.images.edit_enabled
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


def validate_compaction(cfg: Config, proposed: dict) -> list[dict]:
    elide = proposed.get("compaction.elide_at", cfg.elide_at)
    summarize = proposed.get("compaction.summarize_at", cfg.summarize_at)
    keep = proposed.get("compaction.keep_recent", cfg.keep_recent)
    errors = []
    if not (elide < summarize):
        errors.append({"key": "compaction.summarize_at", "code": "cross_field",
                       "message": "compaction.summarize_at must be greater than compaction.elide_at"})
    if not (keep < summarize):
        errors.append({"key": "compaction.keep_recent", "code": "cross_field",
                       "message": "compaction.keep_recent must be less than compaction.summarize_at"})
    return errors


def validate_enables(cfg: Config, proposed: dict) -> list[dict]:
    errors = []
    for key, check in ENABLE_CHECKS.items():
        if key not in proposed:
            continue
        if proposed[key] is not True:
            continue
        spec_name = key.split(".", 1)[0]
        module = "image_edit" if key == "images.edit_enabled" else (
            "notifications" if spec_name == "notifications" else spec_name)
        if not module_installed(cfg, module):
            errors.append({"key": key, "code": "dependency",
                           "message": f"{module} is not installed for this profile"})
            continue
        for message in check(cfg):
            errors.append({"key": key, "code": "dependency", "message": message})
    return errors


def _int(key, label, help, category, default, getter, setter, minimum, maximum, yaml_path,
         apply_mode="live", modules=(), live_apply=None, live_undo=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="int", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(minimum=minimum, maximum=maximum), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_undo,
    )


def _float(key, label, help, category, default, getter, setter, minimum, maximum, yaml_path,
           apply_mode="live", modules=(), live_apply=None, live_undo=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="float", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(minimum=minimum, maximum=maximum), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_undo,
    )


def _bool(key, label, help, category, default, getter, setter, yaml_path, apply_mode="live",
          modules=(), enable_check=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="bool", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter, yaml_path=yaml_path,
        modules=modules, enable_check=enable_check,
    )


def _enum(key, label, help, category, default, getter, setter, values, yaml_path, modules=(),
          apply_mode="live", max_length=80, live_apply=None, live_undo=None):
    return SettingSpec(
        key=key, label=label, help=help, category=category, value_type="enum", default=default,
        scope="admin", apply_mode=apply_mode, getter=getter, setter=setter,
        bounds=Bounds(enum=values, max_length=max_length), yaml_path=yaml_path, modules=modules,
        live_apply=live_apply, live_undo=live_undo,
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


def _get_img_start(cfg: Config):
    return cfg.images.start_timeout_seconds


def _set_img_start(cfg: Config, value):
    cfg.images.start_timeout_seconds = float(value)


def _get_img_job(cfg: Config):
    return cfg.images.job_timeout_seconds


def _set_img_job(cfg: Config, value):
    cfg.images.job_timeout_seconds = float(value)


def _get_img_upload_bytes(cfg: Config):
    return cfg.images.max_upload_bytes


def _set_img_upload_bytes(cfg: Config, value):
    cfg.images.max_upload_bytes = int(value)


def _get_img_pixels(cfg: Config):
    return cfg.images.max_pixels


def _set_img_pixels(cfg: Config, value):
    cfg.images.max_pixels = int(value)


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
    _float("compaction.summarize_at", "Summarize at",
           "Fraction of context at which older turns are summarized. Must be greater than elide_at.",
           "Compaction", 0.65, _get_summarize, _set_summarize, 0.15, 0.95, ("compaction", "summarize_at")),
    _float("compaction.keep_recent", "Keep recent",
           "Fraction of context kept verbatim after a summary. Must be less than summarize_at.",
           "Compaction", 0.20, _get_keep, _set_keep, 0.05, 0.50, ("compaction", "keep_recent")),
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
         live_apply=apply_cleanup_interval, live_undo=apply_cleanup_interval),
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
         modules=("endpoint",), live_apply=apply_endpoint_queue, live_undo=apply_endpoint_queue),
    _float("endpoint.agent_fair_seconds", "Agent fairness (seconds)",
           "After an agent turn waits this long, new endpoint requests queue behind it.",
           "Endpoint", 90, _get_fair, _set_fair, 10, 600, ("endpoint", "agent_fair_seconds"),
           modules=("endpoint",), live_apply=apply_endpoint_queue, live_undo=apply_endpoint_queue),
    _float("endpoint.request_timeout_seconds", "Endpoint request timeout (seconds)",
           "Give up on a hung inference request after this long.",
           "Endpoint", 1800, _get_req_timeout, _set_req_timeout, 30, 7200,
           ("endpoint", "request_timeout_seconds"), modules=("endpoint",)),
    _float("images.start_timeout_seconds", "Image startup timeout (seconds)",
           "How long to wait for ComfyUI to become ready.",
           "Images", 180, _get_img_start, _set_img_start, 30, 600, ("images", "start_timeout_seconds"),
           modules=("images",)),
    _float("images.job_timeout_seconds", "Image job timeout (seconds)",
           "How long a single image job may run.",
           "Images", 1200, _get_img_job, _set_img_job, 60, 7200, ("images", "job_timeout_seconds"),
           modules=("images",)),
    _int("images.max_upload_bytes", "Image-edit upload byte limit",
         "Maximum source or mask upload size for owner-only masked editing.",
         "Images", 20 * 2**20, _get_img_upload_bytes, _set_img_upload_bytes,
         2**20, 100 * 2**20, ("images", "max_upload_bytes"), modules=("image_edit",)),
    _int("images.max_pixels", "Image-edit decoded pixel limit",
         "Reject gallery edits and decoded uploads/masks above this pixel count (long side is also capped at 1664).",
         "Images", 20_000_000, _get_img_pixels, _set_img_pixels,
         1_000_000, 100_000_000, ("images", "max_pixels"), modules=("image_edit",)),
    _float("gpu_guard.poll_seconds", "GPU guard poll (seconds)",
           "How often the GPU guard looks for games or Plex transcodes.",
           "GPU guard", 10, _get_gpu_poll, _set_gpu_poll, 2, 60, ("gpu_guard", "poll_seconds"),
           modules=("gpu_guard",)),
    _float("gpu_guard.resume_after_seconds", "GPU resume delay (seconds)",
           "The GPU must stay clear this long before the model is reloaded.",
           "GPU guard", 180, _get_gpu_resume, _set_gpu_resume, 10, 1800,
           ("gpu_guard", "resume_after_seconds"), modules=("gpu_guard",)),
    _float("gpu_guard.drain_timeout_seconds", "GPU drain timeout (seconds)",
           "Longest wait for the current model turn before the server is stopped.",
           "GPU guard", 300, _get_gpu_drain, _set_gpu_drain, 30, 1800,
           ("gpu_guard", "drain_timeout_seconds"), modules=("gpu_guard",)),
    _float("jobs.poll_seconds", "Job polling interval (seconds)",
           "How often scheduled jobs are checked.",
           "Jobs", 30, _get_jobs_poll, _set_jobs_poll, 5, 300, ("jobs", "poll_seconds"),
           modules=("jobs",), live_apply=apply_jobs_poll, live_undo=apply_jobs_poll),
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
    _bool("smart_approvals.enabled", "Smart approvals",
          "Runtime enable for the hosted smart-approval reviewer. Does not configure a secret_ref.",
          "Smart approvals", False, _get_smart_enabled, _set_smart_enabled,
          ("smart_approvals", "enabled")),
    _enum("smart_approvals.mode", "Smart-approval mode",
          "off, shadow, or auto. Last writer among this setting and PUT /smart-approvals "
          "wins; off means no reviewer calls.",
          "Smart approvals", "shadow", _get_smart_mode, _set_smart_mode,
          ("off", "shadow", "auto"), ("smart_approvals", "mode"),
          live_apply=apply_smart_mode, live_undo=apply_smart_mode),
    _enum("smart_approvals.provider", "Smart-approval provider",
          "Hosted reviewer provider. openai or anthropic.",
          "Smart approvals", "openai", _get_smart_provider, _set_smart_provider,
          ("openai", "anthropic"), ("smart_approvals", "provider")),
    SettingSpec(
        key="smart_approvals.model", label="Smart-approval model",
        help="Hosted reviewer model id. Existing in-flight reviews keep the model they started with.",
        category="Smart approvals", value_type="string", default="gpt-4.1-mini", scope="admin",
        apply_mode="live", getter=_get_smart_model, setter=_set_smart_model,
        bounds=Bounds(min_length=1, max_length=80), yaml_path=("smart_approvals", "model"),
    ),
    _float("smart_approvals.timeout_seconds", "Smart-approval timeout (seconds)",
           "Give up on a hung reviewer request after this long.",
           "Smart approvals", 8, _get_smart_timeout, _set_smart_timeout, 0.1, 60,
           ("smart_approvals", "timeout_seconds")),
    _float("smart_approvals.min_confidence", "Smart-approval min confidence",
           "Hosted reviewer must meet this confidence before auto mode may approve.",
           "Smart approvals", 0.85, _get_smart_confidence, _set_smart_confidence, 0, 1,
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
    _bool("images.enabled", "Image generation",
          "Runtime enable for local image generation.",
          "Features", False, _enable_get("images"), _enable_set("images"), ("images", "enabled"),
          apply_mode="daemon_restart", modules=("images",), enable_check=check_images),
    _bool("images.edit_enabled", "Masked image editing",
          "Runtime enable for the installed Qwen-Image-Edit component. Does not download model weights.",
          "Features", False, _enable_get("image_edit"), _enable_set("image_edit"),
          ("images", "edit_enabled"), apply_mode="daemon_restart", modules=("image_edit",),
          enable_check=check_image_edit),
    _bool("gpu_guard.enabled", "GPU guard",
          "Runtime enable for pausing the model while a game or Plex transcode needs the GPU.",
          "Features", False, _enable_get("gpu_guard"), _enable_set("gpu_guard"), ("gpu_guard", "enabled"),
          apply_mode="daemon_restart", modules=("gpu_guard",), enable_check=check_gpu_guard),
    _bool("notifications.enabled", "Notifications",
          "Runtime enable for ntfy notifications. Does not configure a server or topic.",
          "Features", False, _enable_get("notifications"), _enable_set("notifications"),
          ("notify", "enabled"), apply_mode="daemon_restart", modules=("notifications",),
          enable_check=check_notifications),
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
    _hidden("notify.server", "Notification server", "ntfy server URL.", "Notifications",
            ("notify", "server"), modules=("notifications",)),
    _hidden("notify.topic", "Notification topic", "ntfy topic name.", "Notifications",
            ("notify", "topic"), modules=("notifications",)),
    _hidden("notify.token_file", "Notification token file", "File holding the ntfy write token.",
            "Notifications", ("notify", "token_file"), modules=("notifications",)),
    _hidden("smart_approvals.secret_ref", "Smart-approval secret",
            "Opaque name of the hosted reviewer key file. Managed in local configuration.",
            "Smart approvals", ("smart_approvals", "secret_ref")),
    _hidden("smart_approvals.proxy", "Smart-approval proxy",
            "Optional explicit proxy for the hosted reviewer. Managed in local configuration.",
            "Smart approvals", ("smart_approvals", "proxy")),
    _hidden("install.profile", "Install profile", "full or service. Chosen by the installer.", "Install",
            ("profile",)),
]

for _mod in MODULE_NAMES:
    STATIC_ADMIN.append(_hidden(
        f"modules.{_mod}", f"Module: {_mod}",
        f"{_mod} installation/profile selection. Change this with the installer, not this registry.",
        "Install", ("modules", _mod),
    ))


APP_SPECS: list[SettingSpec] = [
    SettingSpec(
        key="app.default_backend", label="Default backend",
        help="Used only when a session request omits backend. Cannot select an unassigned provider.",
        category="App defaults", value_type="string", default="", scope="app", apply_mode="live",
        getter=_app_get("app.default_backend", ""), setter=_app_set("app.default_backend"),
        bounds=Bounds(max_length=40), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.default_model", label="Default model",
        help="Used only when a session request omits model.",
        category="App defaults", value_type="string", default="", scope="app", apply_mode="live",
        getter=_app_get("app.default_model", ""), setter=_app_set("app.default_model"),
        bounds=Bounds(max_length=80), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.default_effort", label="Default effort",
        help="Used only when a hosted-backend session request omits effort.",
        category="App defaults", value_type="enum", default="", scope="app", apply_mode="live",
        getter=_app_get("app.default_effort", ""), setter=_app_set("app.default_effort"),
        bounds=Bounds(enum=("",) + EFFORTS), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.sessions.max_turns", label="Maximum turns",
        help="Per-session turn cap, never higher than the owner limit.",
        category="App budgets", value_type="int", default=None, scope="app", apply_mode="live",
        getter=_app_get("app.sessions.max_turns", None), setter=_app_set("app.sessions.max_turns"),
        bounds=Bounds(minimum=1, maximum=500), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.sessions.max_completion_tokens", label="Maximum completion tokens",
        help="Per-session completion-token cap, never higher than the owner limit.",
        category="App budgets", value_type="int", default=None, scope="app", apply_mode="live",
        getter=_app_get("app.sessions.max_completion_tokens", None),
        setter=_app_set("app.sessions.max_completion_tokens"),
        bounds=Bounds(minimum=1000, maximum=2_000_000), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.capabilities", label="Enabled capabilities",
        help="Subset of capabilities already granted by the token, installed by the daemon, and allowed by policy.",
        category="App capabilities", value_type="string_list", default=None, scope="app", apply_mode="live",
        getter=_app_get("app.capabilities", None), setter=_app_set("app.capabilities"),
        bounds=Bounds(enum=APP_CAPABILITIES), capabilities=("sessions",),
    ),
    SettingSpec(
        key="app.notify.completion", label="Completion notifications",
        help="inherit uses the owner channel; never silences this app's session-completion notifications.",
        category="App notifications", value_type="enum", default="inherit", scope="app", apply_mode="live",
        getter=_app_get("app.notify.completion", "inherit"), setter=_app_set("app.notify.completion"),
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


def build_registry(cfg: Config) -> Registry:
    specs = {spec.key: spec for spec in [*STATIC_ADMIN, *backend_specs(cfg), *APP_SPECS]}
    return Registry(specs=specs, validators=[validate_compaction, validate_enables])


def assert_explicit_registry(registry: Registry) -> None:
    """Coverage helper: every spec has an explicit getter/setter and metadata, no generic apply modes."""
    for spec in registry.specs.values():
        assert spec.key and "." in spec.key
        assert spec.label and spec.help and spec.category
        assert spec.value_type in ("bool", "int", "float", "string", "enum", "string_list")
        assert spec.scope in ("app", "admin")
        assert spec.apply_mode in APPLY_MODES
        assert spec.sensitivity in ("public", "redact", "hidden")
        assert callable(spec.getter) and callable(spec.setter)
        assert spec.getter.__name__ != "<lambda>" or spec.key.startswith("backends.")
        assert spec.setter.__name__ != "<lambda>" or spec.key.startswith("backends.")
        name = spec.getter.__name__
        assert "getattr" not in name
