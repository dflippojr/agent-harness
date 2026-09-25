"""Public status, notices, and billing decisions for hosted CLI backends."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

EFFORTS = ("low", "medium", "high")
PREFS_KEY = "backend_prefs"
# Few currently popular CLI model ids per hosted backend. The PWA offers these plus a Custom field.
POPULAR_MODELS = {
    "claude": (
        ("claude-opus-5", "Opus 5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-haiku-4-5", "Haiku 4.5"),
    ),
    "codex": (
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
    ),
    "cursor": (
        ("cursor-grok-4.6-high", "Grok 4.6"),
        ("composer-2.5-fast", "Composer 2.5"),
        ("muse-spark-1.3-high", "Muse Spark 1.3"),
    ),
}


def billing_warning(cfg, limits: dict | None = None, using_api_key: bool = False) -> str:
    limits = limits or {}
    status = str(limits.get("status") or "").lower()
    if using_api_key:
        return "This run is using an API key and may incur provider API charges."
    if cfg.billing == "credits" or limits.get("isUsingOverage") or "credit" in status or "overage" in status:
        return "Programmatic usage is drawing from separate credits or overage and may incur provider charges."
    return ""


def notice(name: str, cfg, limits: dict | None = None) -> str:
    base = (f"Runs the unmodified {name.title()} CLI programmatically on this user's machine. "
            "Use must follow the provider's terms; each user signs in and is responsible for usage. ")
    warning = billing_warning(cfg, limits)
    billing = warning or "Current configuration treats usage as part of the subscription; reported cost is an estimate."
    return base + billing + " An API key can be configured as the default or limit fallback."


def _probe_subscription(name: str, cfg) -> bool:
    commands = {
        "claude": ["claude", "auth", "status"],
        "codex": ["codex", "login", "status"],
        "cursor": ["agent", "status"],
    }
    dirs = {"claude": "/home/agent/.claude", "codex": "/home/agent/.codex", "cursor": "/home/agent/.cursor"}
    if name not in commands:
        return False
    command = ["docker", "run", "--rm", "--network", cfg.network,
               "-e", f"HTTPS_PROXY={cfg.proxy}", "-e", "NODE_USE_ENV_PROXY=1",
               "-v", f"{cfg.volume}:{dirs[name]}", cfg.image, *commands[name]]
    if name == "cursor":
        at = command.index(cfg.image)
        command[at:at] = ["-e", "HOME=/home/agent/.cursor/home",
                          "-e", "CURSOR_CONFIG_DIR=/home/agent/.cursor/config"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    if name == "claude":
        try:
            return bool(json.loads(result.stdout).get("loggedIn"))
        except (ValueError, AttributeError):
            return False
    return "not logged in" not in result.stdout.lower()


_AUTH_TTL = 45.0
_auth_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def _subscription_status(name: str, cfg) -> bool:
    key = (str(getattr(cfg, "volume", "")), name)
    now = time.time()
    cached = _auth_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    ok = _probe_subscription(name, cfg)
    _auth_cache[key] = (now + _AUTH_TTL, ok)
    return ok


def local_view(manager) -> dict:
    available = bool(manager.cfg.modules.local_model and manager.cfg.models)
    return {
        "name": "local", "available": available, "logged_in": available, "auth": "local", "billing": "local",
        "model": manager.cfg.default_model, "effort": "",
        "limits": {}, "today": {}, "week": {}, "notice": "Runs the local model on this server.",
        "billing_warning": "", "api_key_available": False,
    }


def _credential_state(cfg, provider_policy: dict | None, check_auth: bool) -> tuple[bool, str, bool]:
    """(allowed, effective auth mode, api key ready) for a backend under an optional app provider policy."""
    if provider_policy and provider_policy["managed"]:
        allowed = provider_policy["allowed"]
        effective_auth = provider_policy["policy"] if allowed else "denied"
        key_ready = provider_policy["available"] if provider_policy["credential_source"] == "app_file" else False
        return allowed, effective_auth, key_ready
    # An unauthenticated discovery response must not reveal whether the owner has a key file.
    key_ready = bool(check_auth and cfg.api_key_file and Path(cfg.api_key_file).is_file())
    return True, cfg.auth, key_ready


def view(manager, name: str, check_auth: bool = True, app_id: str | None = None,
         include_usage: bool = True) -> dict:
    cfg = manager.cfg.backends[name]
    state = manager.db.get_backend_usage(name)
    now = time.time()
    local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week = now - 7 * 86400
    provider_policy = manager.app_provider_status(app_id, name) if app_id is not None else None
    allowed, effective_auth, key_ready = _credential_state(cfg, provider_policy, check_auth)
    subscription = (_subscription_status(name, cfg)
                    if check_auth and effective_auth not in ("api_key", "denied") else False)
    logged_in = key_ready if effective_auth == "api_key" else subscription
    if effective_auth == "subscription_then_api_key":
        logged_in = subscription or key_ready
    # Machine-wide provider limits may describe another app's isolated key. Managed apps get their own
    # rate-limit events on their sessions instead of this shared cache; public discovery gets no limit data.
    hide_shared_limits = not include_usage or bool(provider_policy and provider_policy["managed"])
    limits = {} if hide_shared_limits else state["data"]
    today = manager.db.usage_tally(name, local_midnight, app_id) if include_usage else {}
    week_tally = manager.db.usage_tally(name, week, app_id) if include_usage else {}
    sources = manager.db.usage_by_source(name, week, app_id) if include_usage else {}
    return {
        "name": name, "available": cfg.enabled and allowed, "logged_in": logged_in, "auth": cfg.auth,
        "billing": cfg.billing, "model": cfg.model, "effort": cfg.effort,
        "limits": limits, "limits_updated_at": None if hide_shared_limits else state["updated_at"],
        "today": today, "week": week_tally, "usage_by_source": sources,
        "provider_policy": provider_policy,
        "notice": notice(name, cfg, limits),
        "billing_warning": billing_warning(cfg, limits, effective_auth == "api_key"),
        "api_key_available": key_ready,
        "popular_models": [{"id": model_id, "label": label} for model_id, label in POPULAR_MODELS.get(name, ())],
    }


def _prefs(manager) -> dict:
    raw = manager.db.get_meta(PREFS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def apply_prefs(manager) -> None:
    """Overlay Settings → Backends model/effort choices onto the in-memory config."""
    for name, spec in _prefs(manager).items():
        if isinstance(spec, dict):
            _apply_pref(manager, name, spec)


def _apply_pref(manager, name: str, spec: dict) -> None:
    model = str(spec.get("model") or "").strip()[:80]
    effort = str(spec.get("effort") or "").strip()
    if name == "local":
        if model in manager.cfg.models:
            manager.cfg.default_model = model
    elif name in manager.cfg.backends:
        if model:
            manager.cfg.backends[name].model = model
        if effort in EFFORTS:
            manager.cfg.backends[name].effort = effort


def _save_prefs_via_settings(manager, settings, name: str, model: str | None, effort: str | None) -> dict:
    changes = {}
    if name == "local":
        if model is not None:
            changes["backends.local.model"] = model
    elif f"backends.{name}.model" in settings.registry.specs or name in manager.cfg.backends:
        if model is not None:
            changes[f"backends.{name}.model"] = model
        if effort is not None:
            changes[f"backends.{name}.effort"] = effort
    else:
        raise KeyError(name)
    if not changes:
        raise ValueError("set model or effort")
    try:
        settings.patch_admin(changes, settings.admin_view()["revision"])
    except Exception as e:
        from .settings_service import SettingsError
        if isinstance(e, SettingsError):
            raise ValueError(str(e)) from e
        raise
    if name == "local":
        return {"model": manager.cfg.default_model}
    backend = manager.cfg.backends[name]
    return {"model": backend.model, "effort": backend.effort}


def _update_local_pref(manager, spec: dict, model: str | None) -> None:
    if not manager.cfg.modules.local_model:
        raise ValueError("the local model is disabled by this service profile")
    if model is not None:
        model = model.strip()
        if model not in manager.cfg.models:
            raise ValueError(f"unknown model {model!r}; known: {', '.join(manager.cfg.models)}")
        spec["model"] = model
        spec.pop("effort", None)


def _update_hosted_pref(spec: dict, model: str | None, effort: str | None) -> None:
    if model is not None:
        model = model.strip()[:80]
        if not model:
            raise ValueError("model is empty")
        spec["model"] = model
    if effort is not None:
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
        spec["effort"] = effort


def save_prefs(manager, name: str, model: str | None = None, effort: str | None = None) -> dict:
    """Persist a default model/effort for `name` (`local` or a hosted backend) and apply it now."""
    settings = getattr(manager, "settings", None)
    if settings is not None:
        return _save_prefs_via_settings(manager, settings, name, model, effort)
    prefs = _prefs(manager)
    spec = dict(prefs.get(name) or {})
    if name == "local":
        _update_local_pref(manager, spec, model)
    elif name in manager.cfg.backends:
        _update_hosted_pref(spec, model, effort)
    else:
        raise KeyError(name)
    prefs[name] = spec
    manager.db.set_meta(PREFS_KEY, json.dumps(prefs))
    apply_prefs(manager)
    return spec
