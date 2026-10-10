"""Safe inventory projections; reads main-store metadata only."""
from __future__ import annotations

import asyncio
from dataclasses import fields
import inspect
import time

from .entries import load

STATES = ("active", "idle", "stale", "never_used", "revoked", "erasure_pending")
KEY_FIELDS = ("id", "name", "prefix", "kind", "role", "scopes", "origins", "catalog_app_id", "created_at",
              "last_used_at", "revoked_at", "erase_after", "erased_at")
STORE_FIELDS = ("sessions", "usage", "errors", "last_error", "last_error_at")


def connection_state(row, now):
    if row.get("erase_after") is not None and row.get("erased_at") is None:
        return "erasure_pending"
    if row.get("revoked_at") is not None:
        return "revoked"
    used = row.get("last_used_at")
    if used is None:
        return "never_used"
    age = max(0, now - used)
    return "active" if age <= 15 * 60 else "idle" if age <= 7 * 86400 else "stale"


def apps(db, now):
    out = []
    for row in db.list_api_keys():
        item = {key: row.get(key) for key in KEY_FIELDS}
        store = row.get("store") or {}
        item["store"] = {key: store.get(key) for key in STORE_FIELDS}
        item["state"] = connection_state(row, now)
        created = row.get("created_at")
        item["token_age_seconds"] = max(0, now - created) if created is not None else None
        last_error = store.get("last_error_at")
        item["errors"] = bool(last_error is not None and 0 <= now - last_error <= 86400)
        out.append(item)
    return out


def _snapshot(manager, now):
    paired = apps(manager.db, now)
    pending = set(manager.db.main.pending_pairing_catalog_ids(now))
    entries = []
    for manifest in load(manager.cfg.config_dir / "hub.entries.json"):
        app_id = manifest["app"]["app_id"]
        ids = [app["id"] for app in paired if app["catalog_app_id"] == app_id]
        entries.append({"catalog_app_id": app_id, "manifest": manifest, "verified": False,
                        "state": "paired" if ids else "not_paired", "paired": ids,
                        "pending_pairing": app_id in pending})
    return paired, entries


async def _detail(rt):
    try:
        if inspect.iscoroutinefunction(rt.status):
            return await asyncio.wait_for(rt.status(), timeout=6)
        return await asyncio.to_thread(rt.status)
    except Exception:
        # Exception strings and traces may contain credentials; never return or log them.
        return {"state": "error"}


async def inventory(manager):
    now = time.time()
    paired, entries = await asyncio.to_thread(_snapshot, manager, now)
    caps = manager.cfg.capabilities()["modules"]
    details = {}
    for rt in manager.modules:
        if rt.effective():
            details[rt.module.name] = await _detail(rt)
    module_rows = []
    names = dict.fromkeys([field.name for field in fields(manager.cfg.installed)] + list(caps))
    for name in names:
        present = name in caps
        row = {"name": name, "state": "present" if caps.get(name) else "switched_off" if present else "absent"}
        rt = next((rt for rt in manager.modules if name in rt.module.switches), None)
        detail = details.get(rt.module.name) if rt else None
        if detail is not None:
            row["status"] = detail
        module_rows.append(row)
    return {"modules": module_rows, "apps": paired, "entries": entries}
