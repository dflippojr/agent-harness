"""Safe inventory projections; reads main-store metadata only."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import fields
import inspect
from itertools import islice
import math
import time
import threading

from harness.modules import normalize_origin

from .entries import load

STATES = ("active", "idle", "stale", "never_used", "revoked", "erasure_pending")
KEY_FIELDS = ("id", "name", "prefix", "kind", "role", "scopes", "origins", "catalog_app_id", "created_at",
              "last_used_at", "revoked_at", "erase_after", "erased_at")
STORE_FIELDS = ("sessions", "usage", "errors", "last_error", "last_error_at")
STATUS_TIMEOUT = 6
STATUS_FIELDS = 8
_STATUS_LOCK = threading.Lock()


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
    pending = manager.db.main.pending_pairing_catalog_origins(now)
    entries = []
    for manifest in load(manager.cfg.config_dir / "hub.entries.json"):
        app_id = manifest["app"]["app_id"]
        origins = {normalize_origin(origin) for origin in manifest["app"]["browser_origins"]}
        labelled = [app for app in paired if app["catalog_app_id"] == app_id]
        ids = [app["id"] for app in labelled if app["origins"] and set(app["origins"]) <= origins]
        entries.append({"catalog_app_id": app_id, "manifest": manifest, "verified": False,
                        "state": "paired" if ids else "not_paired", "paired": ids,
                        "pending_pairing": any(label == app_id and origin in origins for label, origin in pending),
                        "unverified_origin": {
                            "paired": [app["id"] for app in labelled if not app["origins"]],
                            "pending_pairing": any(label == app_id and not origin for label, origin in pending)}})
    return paired, entries


def _bounded_status(detail):
    """Only the first eight fields, with short strings and finite JSON scalar values."""
    if not isinstance(detail, dict):
        return None
    out = {}
    for key, value in islice(detail.items(), STATUS_FIELDS):
        if type(key) is not str or len(key) > 200:
            continue
        if type(value) is str:
            out[key] = value[:200]
        elif value is None or type(value) is bool:
            out[key] = value
        elif type(value) is int and value.bit_length() <= 64:
            out[key] = value
        elif type(value) is float and math.isfinite(value):
            out[key] = value
    return out or None


def _sync_probe(rt):
    """One daemon thread per runtime, retained after timeout; never occupy the shared executor."""
    with _STATUS_LOCK:
        probe = getattr(rt, "_hub_status_probe", None)
        if probe is None or probe.done():
            probe = Future()
            rt._hub_status_probe = probe

            def run():
                try:
                    probe.set_result(_bounded_status(rt.status()))
                except Exception as error:
                    probe.set_exception(error)

            threading.Thread(target=run, name="hub-status", daemon=True).start()
        return probe


def _sync_waiter(rt):
    probe = _sync_probe(rt)
    cached = getattr(rt, "_hub_status_waiter", None)
    if cached is None or cached[0] is not probe or cached[1].get_loop() is not asyncio.get_running_loop():
        cached = (probe, asyncio.wrap_future(probe))
        rt._hub_status_waiter = cached
        # A hook can raise after every waiter timed out; consume the exception without logging secrets.
        cached[1].add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
    return cached[1]


async def _detail(rt):
    try:
        if inspect.iscoroutinefunction(rt.status):
            detail = await asyncio.wait_for(rt.status(), timeout=STATUS_TIMEOUT)
        else:
            detail = await asyncio.wait_for(asyncio.shield(_sync_waiter(rt)), timeout=STATUS_TIMEOUT)
        return _bounded_status(detail)
    except Exception:
        # Exception strings and traces may contain credentials; never return or log them.
        return {"state": "error"}


async def inventory(manager):
    now = time.time()
    paired, entries = await asyncio.to_thread(_snapshot, manager, now)
    caps = manager.cfg.capabilities()["modules"]
    runtimes = [rt for rt in manager.modules if rt.effective()]
    results = await asyncio.gather(*(_detail(rt) for rt in runtimes))
    details = {rt.module.name: detail for rt, detail in zip(runtimes, results)}
    module_rows = []
    names = dict.fromkeys([field.name for field in fields(manager.cfg.installed)] + list(caps))
    for name in names:
        present = name in caps
        row = {"name": name, "state": "present" if caps.get(name) else "switched_off" if present else "absent"}
        rt = next((rt for rt in manager.modules if name in rt.module.switches), None)
        detail = details.get(rt.module.name) if rt and caps.get(name) else None
        if detail is not None:
            row["status"] = detail
        module_rows.append(row)
    return {"modules": module_rows, "apps": paired, "entries": entries}
