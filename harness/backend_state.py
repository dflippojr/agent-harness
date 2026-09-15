"""Public status, notices, and billing decisions for hosted CLI backends."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path


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


def _subscription_status(name: str, cfg) -> bool:
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


def view(manager, name: str, check_auth: bool = True) -> dict:
    cfg = manager.cfg.backends[name]
    state = manager.db.get_backend_usage(name)
    now = time.time()
    local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week = now - 7 * 86400
    key_ready = bool(cfg.api_key_file and Path(cfg.api_key_file).is_file())
    subscription = _subscription_status(name, cfg) if check_auth and cfg.auth != "api_key" else False
    logged_in = key_ready if cfg.auth == "api_key" else subscription
    if cfg.auth == "subscription_then_api_key":
        logged_in = subscription or key_ready
    limits = state["data"]
    return {
        "name": name, "available": cfg.enabled, "logged_in": logged_in, "auth": cfg.auth,
        "billing": cfg.billing, "model": cfg.model, "limits": limits, "limits_updated_at": state["updated_at"],
        "today": manager.db.usage_tally(name, local_midnight),
        "week": manager.db.usage_tally(name, week), "notice": notice(name, cfg, limits),
        "billing_warning": billing_warning(cfg, limits), "api_key_available": key_ready,
    }
