"""Apply, validate, persist, restart, and roll back registered configuration."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .managed_config import Envelope, ManagedConfigError, ManagedStore
from .settings import (
    RESET, SCHEMA_VERSION, SettingSpec, apply_spec, copy_cfg, inherited_source, looks_hidden,
    parse_value, redact_value, schema_entry, spec_available, supervised_restart_supported,
)
from .settings_keys import APP_SPECS, build_registry

log = logging.getLogger("harness.settings")

YAML_NAMES = ("harness.yaml", "harness.local.yaml", "profile.yaml")


class SettingsError(Exception):
    def __init__(self, status: int, message: str, code: str, keys: dict | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.keys = keys or {}
        self.details = details or {}


@dataclass
class Change:
    key: str
    before: Any
    after: Any
    apply: str
    action: str  # set | reset
    source: str = "managed"


@dataclass
class Plan:
    revision: int
    target_revision: int
    changes: list[Change] = field(default_factory=list)
    restart_required: bool = False
    live: list[Change] = field(default_factory=list)
    pending: list[Change] = field(default_factory=list)
    errors: dict[str, dict] = field(default_factory=dict)

    def as_dict(self, registry) -> dict[str, Any]:
        def row(change: Change) -> dict[str, Any]:
            spec = registry.specs.get(change.key)
            return {
                "key": change.key,
                "from": redact_value(change.key, spec, change.before),
                "to": None if change.action == "reset" else redact_value(change.key, spec, change.after),
                "apply": change.apply,
                "action": change.action,
            }
        return {
            "revision": self.revision,
            "target_revision": self.target_revision,
            "restart_required": self.restart_required,
            "changes": [row(change) for change in self.changes],
            "errors": self.errors,
        }


@dataclass
class AppEnvelope:
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


class SettingsService:
    def __init__(self, cfg: Config, db=None, manager=None):
        self.cfg = cfg
        self.db = db
        self.manager = manager
        self.store = ManagedStore(cfg.data_dir)
        self.registry = build_registry(cfg)
        self.yaml_files = _load_yaml_files(getattr(cfg, "config_dir", None))
        self.inherited: dict[str, Any] = {}
        self.sources: dict[str, str] = {}
        self.audit_path = cfg.data_dir / "config-audit.jsonl"
        self._restarting = False
        self._lock = threading.RLock()
        self._capture_inherited()

    def _capture_inherited(self) -> None:
        preset = getattr(self.cfg, "_inherited", None)
        for spec in self.registry.writable_admin():
            if preset is not None and spec.key in preset:
                self.inherited[spec.key] = _clone(preset[spec.key])
            else:
                try:
                    self.inherited[spec.key] = _clone(spec.getter(self.cfg))
                except Exception:
                    self.inherited[spec.key] = spec.default
            self.sources[spec.key] = inherited_source(spec, self.yaml_files)

    # --- load / recovery -------------------------------------------------
    def apply_overlay(self) -> dict[str, Any]:
        """Apply the active managed overlay, restoring LKG if a candidate is unconfirmed or invalid."""
        status: dict[str, Any] = {"recovery": None}
        with self.store.lock():
            try:
                active = self.store.read_active()
            except ManagedConfigError as e:
                restored = self.store.restore_lkg(str(e))
                self._audit("system", "lkg_recovery", [], "failure", extra={"reason": str(e)})
                if restored is None:
                    raise
                active = restored
                status["recovery"] = self.store.read_status().get("recovery")
            if active is None:
                self._migrate_backend_prefs()
                return status
            if active.unconfirmed or not active.confirmed:
                # boot-tried is a cross-process crash flag. load() then Manager.__init__ both
                # call apply_overlay on the same cfg; the in-process marker keeps the first
                # start from quarantining its own candidate.
                tried_here = getattr(self.cfg, "_managed_boot_attempt", False)
                if self.store.boot_tried() and not tried_here:
                    restored = self.store.restore_lkg("unconfirmed managed generation did not finish startup")
                    self._audit("system", "lkg_recovery", list((active.values or {}).keys()), "ok",
                                extra={"reason": "unconfirmed", "revision": active.revision})
                    active = restored
                    status["recovery"] = "lkg_restore"
                    if active is None:
                        return status
                else:
                    self.store.mark_boot_tried()
                    self.cfg._managed_boot_attempt = True
            try:
                self._apply_values(self.cfg, active.values, persist=False)
            except SettingsError as e:
                restored = self.store.restore_lkg(str(e))
                self._audit("system", "lkg_recovery", list(active.values), "failure", extra={"reason": str(e)})
                status["recovery"] = "lkg_restore"
                if restored is not None:
                    self._apply_values(self.cfg, restored.values, persist=False)
            self._migrate_backend_prefs()
        return status

    def confirm_startup(self) -> None:
        with self.store.lock():
            active = self.store.read_active()
            if active is None:
                return
            if active.unconfirmed or not active.confirmed:
                active.confirmed = True
                active.unconfirmed = False
                self.store.write_active(active)
                self.store.clear_pending()
                self.store.clear_boot_tried()
                self._audit("system", "confirm", list(active.values), "ok", revision=active.revision)

    def _migrate_backend_prefs(self) -> None:
        if self.db is None:
            return
        active = self.store.read_active()
        if active and active.migrated_backend_prefs:
            return
        raw = self.db.get_meta("backend_prefs")
        if not raw:
            if active is not None:
                active.migrated_backend_prefs = True
                self.store.write_active(active)
            return
        try:
            prefs = json.loads(raw)
        except ValueError:
            prefs = {}
        if not isinstance(prefs, dict) or not prefs:
            if active is not None:
                active.migrated_backend_prefs = True
                self.store.write_active(active)
            return
        values = dict(active.values if active else {})
        if "local" in prefs and isinstance(prefs["local"], dict) and prefs["local"].get("model"):
            key = "backends.local.model"
            if key in self.registry.specs and key not in values:
                values[key] = prefs["local"]["model"]
        for name, spec in prefs.items():
            if name == "local" or not isinstance(spec, dict):
                continue
            if spec.get("model") and f"backends.{name}.model" in self.registry.specs:
                values.setdefault(f"backends.{name}.model", spec["model"])
            if spec.get("effort") and f"backends.{name}.effort" in self.registry.specs:
                values.setdefault(f"backends.{name}.effort", spec["effort"])
        envelope = active or Envelope(revision=1, confirmed=True, migrated_backend_prefs=True)
        envelope.values = values
        envelope.migrated_backend_prefs = True
        envelope.revision = max(envelope.revision, 1)
        self.store.write_active(envelope)
        try:
            self._apply_values(self.cfg, envelope.values, persist=False)
        except SettingsError:
            log.exception("backend_prefs migration produced invalid values")
        self._audit("system", "migrate_backend_prefs", list(values), "ok", revision=envelope.revision)

    # --- views -----------------------------------------------------------
    def admin_schema(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "supervised_restart": supervised_restart_supported(),
            "settings": [schema_entry(spec, self.cfg) for spec in self.registry.admin()],
        }

    def app_schema(self, key: dict) -> dict[str, Any]:
        scopes = set((key.get("scopes") or "").split()) | set(key.get("scope_set") or [])
        settings = []
        for spec in APP_SPECS:
            entry = schema_entry(spec, self.cfg)
            entry["writable"] = entry["writable"] and all(cap in scopes or cap == "sessions" and "sessions" in scopes
                                                          for cap in spec.capabilities)
            settings.append(entry)
        return {"schema_version": SCHEMA_VERSION, "settings": settings}

    def admin_view(self) -> dict[str, Any]:
        active = self._active()
        pending = self._pending()
        recovery = self.store.read_status()
        settings = []
        for spec in self.registry.admin():
            settings.append(self._setting_view(spec, active, pending))
        revision = active.revision if active else 0
        return {
            "revision": revision,
            "pending_revision": pending.revision if pending else None,
            "confirmed": True if active is None else bool(active.confirmed and not active.unconfirmed),
            "supervised_restart": supervised_restart_supported(),
            "restart_required": pending is not None,
            "recovery": recovery or None,
            "etag": str(revision),
            "settings": settings,
        }

    def app_view(self, app_id: str, key: dict) -> dict[str, Any]:
        envelope = self._app_envelope(app_id)
        settings = []
        for spec in APP_SPECS:
            settings.append(self._app_setting_view(spec, envelope, key))
        return {
            "revision": envelope.revision,
            "etag": str(envelope.revision),
            "settings": settings,
        }

    def _setting_view(self, spec: SettingSpec, active: Envelope | None, pending: Envelope | None) -> dict[str, Any]:
        inherited = self.inherited.get(spec.key, spec.default)
        configured = None
        source = self.sources.get(spec.key, "default")
        if spec.apply_mode == "installer_only":
            return {
                **schema_entry(spec, self.cfg),
                "configured": None,
                "effective": None,
                "inherited": None,
                "pending": None,
                "source": "file",
                "capped_by": None,
                "guidance": "managed in local configuration",
            }
        if active and spec.key in active.values:
            configured = active.values[spec.key]
            source = "managed"
        effective = spec.getter(self.cfg)
        pending_value = pending.values.get(spec.key) if pending else None
        if pending and spec.key in pending.values and pending.values[spec.key] != effective:
            # pending only differs for restart-required keys not yet in the process
            pass
        return {
            **schema_entry(spec, self.cfg),
            "configured": redact_value(spec.key, spec, configured),
            "effective": redact_value(spec.key, spec, effective),
            "inherited": redact_value(spec.key, spec, inherited),
            "pending": redact_value(spec.key, spec, pending_value) if pending and spec.key in pending.values
            and spec.apply_mode == "daemon_restart" else None,
            "source": source,
            "capped_by": None,
            "available": spec_available(self.cfg, spec),
        }

    def _app_setting_view(self, spec: SettingSpec, envelope: AppEnvelope, key: dict) -> dict[str, Any]:
        configured = envelope.values.get(spec.key, spec.default)
        effective, capped_by = self._effective_app_value(spec, configured, key)
        return {
            **schema_entry(spec, self.cfg),
            "configured": configured,
            "effective": effective,
            "inherited": spec.default,
            "pending": None,
            "source": "app" if spec.key in envelope.values else "default",
            "capped_by": capped_by,
        }

    def _effective_app_value(self, spec: SettingSpec, configured: Any, key: dict) -> tuple[Any, str | None]:
        if spec.key == "app.sessions.max_turns" and configured is not None:
            cap = self.cfg.max_turns
            if int(configured) > cap:
                return cap, "sessions.max_turns"
        if spec.key == "app.sessions.max_completion_tokens" and configured is not None:
            cap = self.cfg.max_completion_tokens
            if int(configured) > cap:
                return cap, "sessions.max_completion_tokens"
        if spec.key == "app.capabilities" and configured is not None:
            allowed = self._allowed_app_capabilities(key)
            filtered = [item for item in configured if item in allowed]
            if filtered != list(configured):
                return filtered, "capabilities"
        if spec.key == "app.default_backend" and configured:
            if not self._backend_allowed(key, configured):
                return "", "provider_policy"
        if spec.key == "app.default_model" and configured:
            backend = (self._app_envelope(key["id"]).values.get("app.default_backend")
                       or ("local" if self.cfg.modules.local_model else ""))
            if backend == "local" and configured not in self.cfg.models:
                return "", "models"
            if backend and backend != "local":
                cred = self.db.app_provider_credential(key["id"], backend) if self.db else None
                if cred and cred.get("models") and configured not in cred["models"]:
                    return "", "provider_policy"
        return configured, None

    def _allowed_app_capabilities(self, key: dict) -> set[str]:
        scopes = set((key.get("scopes") or "").split()) | set(key.get("scope_set") or [])
        allowed = set()
        mapping = {
            "web": ("sessions", "web"),
            "images": ("images", "images"),
            "search": ("sessions", "search"),
            "memory_library": ("sessions", "memory_library"),
            "remote_control": ("remote_control", "remote_control"),
            "homelab": ("sessions", "homelab"),
        }
        for cap, (scope, module) in mapping.items():
            if scope in scopes and module_installed_or_effective(self.cfg, module):
                allowed.add(cap)
        return allowed

    def _backend_allowed(self, key: dict, backend: str) -> bool:
        if backend == "local":
            return bool(self.cfg.modules.local_model and self.cfg.models)
        if backend not in self.cfg.backends or not self.cfg.backends[backend].enabled:
            return False
        if self.db is None:
            return True
        if not self.db.app_provider_managed(key["id"]):
            return True
        return self.db.app_provider_credential(key["id"], backend) is not None

    # --- mutate ----------------------------------------------------------
    def validate_admin(self, changes: dict[str, Any], revision: int | None, actor: dict | None = None) -> Plan:
        return self._plan_admin(changes, revision, persist=False, apply=False, actor=actor)

    def patch_admin(self, changes: dict[str, Any], revision: int | None, dry_run: bool = False,
                    actor: dict | None = None) -> dict[str, Any]:
        plan = self._plan_admin(changes, revision, persist=not dry_run, apply=not dry_run, actor=actor)
        body = plan.as_dict(self.registry)
        if dry_run:
            body["dry_run"] = True
            return body
        body.update({k: self.admin_view()[k] for k in
                     ("revision", "pending_revision", "confirmed", "supervised_restart", "restart_required",
                      "recovery", "etag")})
        body["settings"] = self.admin_view()["settings"]
        return body

    def rollback(self, revision: int | None, dry_run: bool = False, actor: dict | None = None) -> dict[str, Any]:
        with self.store.lock():
            lkg = self.store.read_lkg()
            active = self._active()
            if lkg is None:
                raise SettingsError(409, "no previous confirmed generation to restore", "nothing_to_rollback")
            if revision is not None and active and revision != active.revision:
                raise SettingsError(409, f"configuration revision {revision} is stale; current revision is {active.revision}",
                                    "revision_conflict",
                                    details={"expected_revision": revision, "current_revision": active.revision})
            target = {spec.key: lkg.values[spec.key] if spec.key in lkg.values else RESET
                      for spec in self.registry.writable_admin()
                      if spec.key in (active.values if active else {}) or spec.key in lkg.values}
            # Restore exactly the LKG map: keys not in LKG reset to inherited.
            current_keys = set((active.values if active else {}) | lkg.values)
            changes = {}
            for key in current_keys:
                if key in lkg.values:
                    changes[key] = lkg.values[key]
                else:
                    changes[key] = None
            return self.patch_admin(changes, active.revision if active else 0, dry_run=dry_run, actor=actor)

    def request_restart(self, revision: int | None, actor: dict | None = None) -> dict[str, Any]:
        if not supervised_restart_supported():
            raise SettingsError(
                409,
                "this process is not supervised; stop it yourself and start it again with python -m harness "
                "or the installed supervisor",
                "restart_not_supervised",
                details={"manual": "Stop this process, then start the installed supervisor or run python -m harness."},
            )
        with self.store.lock():
            pending = self._pending()
            active = self._active()
            target = pending or active
            if target is None:
                raise SettingsError(409, "there is no managed configuration to restart into", "nothing_to_restart")
            if revision is not None and pending and revision not in (pending.revision, active.revision if active else None):
                raise SettingsError(409, f"configuration revision {revision} is stale; current revision is {active.revision if active else 0}",
                                    "revision_conflict",
                                    details={"expected_revision": revision,
                                             "current_revision": pending.revision})
            if pending is not None:
                if active is not None and active.confirmed:
                    self.store.write_lkg(active)
                pending.confirmed = False
                pending.unconfirmed = True
                pending.previous_revision = active.revision if active else None
                self.store.write_active(pending)
                self.store.mark_boot_tried()  # cleared after we actually start; first boot after this still tries once
                # First attempt: delete boot-tried so the next process treats this as the first try.
                self.store.clear_boot_tried()
                target_revision = pending.revision
            else:
                target_revision = active.revision if active else 0
            self._audit(_actor_kind(actor), "restart", [], "ok", revision=target_revision, actor=actor)
            self._restarting = True
        return {"accepted": True, "target_revision": target_revision, "status": "restarting"}

    def _plan_admin(self, changes: dict[str, Any], revision: int | None, persist: bool, apply: bool,
                    actor: dict | None) -> Plan:
        if not isinstance(changes, dict):
            raise SettingsError(400, "changes must be an object", "invalid_request")
        with self.store.lock():
            active = self._active() or Envelope()
            pending = self._pending()
            current_revision = active.revision
            if revision is not None and revision != current_revision:
                self._audit(_actor_kind(actor), "revision_conflict", list(changes), "failure",
                            revision=current_revision, actor=actor)
                raise SettingsError(
                    409,
                    f"configuration revision {revision} is stale; current revision is {current_revision}",
                    "revision_conflict",
                    details={"expected_revision": revision, "current_revision": current_revision},
                )
            parsed, errors = self._parse_admin_changes(changes)
            plan = Plan(revision=current_revision, target_revision=current_revision + 1, errors=errors)
            if errors:
                self._audit(_actor_kind(actor), "validate", list(changes), "failure", revision=current_revision,
                            actor=actor, extra={"keys": errors})
                raise SettingsError(400, "configuration is invalid", "validation_error", keys=errors)

            candidate_cfg = copy_cfg(self.cfg)
            merged = dict(active.values)
            restart_merged = dict(pending.values) if pending else dict(active.values)
            for key, value in parsed.items():
                spec = self.registry.get(key)
                before = spec.getter(self.cfg)
                if value is RESET:
                    merged.pop(key, None)
                    restart_merged.pop(key, None)
                    after = self.inherited.get(key, spec.default)
                    action = "reset"
                else:
                    merged[key] = value
                    restart_merged[key] = value
                    after = value
                    action = "set"
                change = Change(key=key, before=before, after=after, apply=spec.apply_mode, action=action)
                plan.changes.append(change)
                if spec.apply_mode == "daemon_restart":
                    plan.pending.append(change)
                    plan.restart_required = True
                else:
                    plan.live.append(change)

            live_values = {key: value for key, value in merged.items()
                           if self.registry.get(key).apply_mode == "live"}
            pending_values = dict(merged)
            # Restart keys stay out of the running process until promotion.
            for spec in self.registry.writable_admin():
                if spec.apply_mode == "daemon_restart" and spec.key in live_values:
                    live_values.pop(spec.key, None)

            try:
                self._apply_values(candidate_cfg, live_values | {
                    key: restart_merged[key] for key in restart_merged
                    if self.registry.get(key).apply_mode == "daemon_restart"
                }, persist=False)
            except SettingsError as e:
                plan.errors = e.keys
                raise

            proposed = {change.key: (None if change.action == "reset" else change.after) for change in plan.changes}
            proposed_applied = {k: v for k, v in proposed.items() if v is not None}
            cross = []
            for validator in self.registry.validators:
                cross.extend(validator(candidate_cfg, proposed_applied))
            if cross:
                keys = {item["key"]: {"code": item["code"], "message": item["message"]} for item in cross}
                raise SettingsError(400, "configuration is invalid", "validation_error", keys=keys)

            if not persist:
                return plan

            previous = copy_cfg(self.cfg)
            previous_active = active
            previous_pending = pending
            live_applied: list[tuple[SettingSpec, Any, Any]] = []
            committed = False
            try:
                new_active = Envelope(
                    revision=plan.target_revision,
                    confirmed=True,
                    values={k: v for k, v in merged.items()
                            if self.registry.get(k).apply_mode == "live"},
                    migrated_backend_prefs=True,
                    previous_revision=current_revision if current_revision else None,
                    updated_at=time.time(),
                )
                # Keep previously live keys that were not reset.
                for key, value in active.values.items():
                    spec = self.registry.specs.get(key)
                    if spec and spec.apply_mode == "live" and key not in parsed:
                        new_active.values[key] = value
                # Also copy restart keys that are already effective (were promoted earlier) into active
                # only after restart. Active file stores all currently effective managed keys plus live updates.
                for key, value in active.values.items():
                    spec = self.registry.specs.get(key)
                    if spec and spec.apply_mode == "daemon_restart" and key not in parsed:
                        new_active.values[key] = value
                for key, value in parsed.items():
                    spec = self.registry.get(key)
                    if spec.apply_mode == "daemon_restart":
                        new_active.values.pop(key, None) if value is RESET else None
                    elif value is RESET:
                        new_active.values.pop(key, None)
                    elif spec.apply_mode == "live":
                        new_active.values[key] = value

                new_pending = None
                pending_map = dict(pending.values) if pending else dict(active.values)
                restart_touched = False
                for key, value in parsed.items():
                    spec = self.registry.get(key)
                    if spec.apply_mode != "daemon_restart":
                        if value is RESET:
                            pending_map.pop(key, None)
                        else:
                            pending_map[key] = value
                        continue
                    restart_touched = True
                    if value is RESET:
                        pending_map.pop(key, None)
                    else:
                        pending_map[key] = value
                # Pending candidate is the full next generation (live+restart).
                full_next = dict(new_active.values)
                for key, value in pending_map.items():
                    spec = self.registry.specs.get(key)
                    if spec and spec.apply_mode == "daemon_restart":
                        full_next[key] = value
                if restart_touched or (pending and any(c.apply == "daemon_restart" for c in plan.changes)):
                    new_pending = Envelope(
                        revision=plan.target_revision,
                        confirmed=False,
                        unconfirmed=True,
                        values=full_next,
                        migrated_backend_prefs=True,
                        previous_revision=current_revision or None,
                        updated_at=time.time(),
                    )

                if current_revision and active.values and active.confirmed:
                    # Keep LKG as last confirmed generation unless this write is itself the first.
                    if not self.store.lkg_exists():
                        self.store.write_lkg(active)

                committed = False
                self.store.write_active(new_active)
                if new_pending is not None:
                    self.store.write_pending(new_pending)
                    plan.restart_required = True
                elif pending is not None and not restart_touched:
                    pending.values = {**pending.values, **new_active.values}
                    pending.revision = plan.target_revision
                    self.store.write_pending(pending)
                elif not restart_touched:
                    self.store.clear_pending()
                committed = True

                if apply:
                    for change in plan.live:
                        spec = self.registry.get(change.key)
                        old = spec.getter(self.cfg)
                        new = self.inherited.get(change.key, spec.default) if change.action == "reset" else change.after
                        spec.setter(self.cfg, new)
                        live_applied.append((spec, old, new))
                        if spec.live_apply and self.manager is not None:
                            spec.live_apply(self.manager, old, new)

                self._audit(_actor_kind(actor), "patch", [c.key for c in plan.changes], "ok",
                            revision=plan.target_revision, actor=actor,
                            extra={"changes": plan.as_dict(self.registry)["changes"]})
                return plan
            except Exception as e:
                for spec, old, new in reversed(live_applied):
                    try:
                        spec.setter(self.cfg, old)
                        if spec.live_undo and self.manager is not None:
                            spec.live_undo(self.manager, new, old)
                    except Exception:
                        log.exception("failed to undo live hook for %s", spec.key)
                try:
                    if committed:
                        if previous_active.revision or previous_active.values:
                            self.store.write_active(previous_active)
                        else:
                            self.store._unlink(self.store.active_path)
                        if previous_pending is not None:
                            self.store.write_pending(previous_pending)
                        else:
                            self.store.clear_pending()
                except Exception:
                    log.exception("failed to restore managed-config after live-hook failure")
                self._audit(_actor_kind(actor), "live_hook_rollback", [c.key for c in plan.changes], "failure",
                            revision=current_revision, actor=actor, extra={"reason": str(e)})
                if isinstance(e, SettingsError):
                    raise
                raise SettingsError(500, f"failed to apply configuration: {e}", "apply_failed") from e

    def _parse_admin_changes(self, changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict]]:
        parsed: dict[str, Any] = {}
        errors: dict[str, dict] = {}
        for key, value in changes.items():
            spec = self.registry.specs.get(key)
            if spec is None:
                errors[key] = {"code": "unknown_key", "message": f"unknown setting {key!r}"}
                continue
            if spec.scope != "admin":
                errors[key] = {"code": "forbidden_key", "message": "app settings cannot be written on the owner API"}
                continue
            if spec.apply_mode == "installer_only" or not spec.writable:
                errors[key] = {"code": "installer_only",
                               "message": "this setting is managed in local configuration"}
                continue
            if not spec_available(self.cfg, spec) and value not in (None, False, RESET):
                errors[key] = {"code": "dependency",
                               "message": "this setting is not available for the current profile or modules"}
                continue
            try:
                parsed[key] = parse_value(spec, value)
            except ValueError as e:
                errors[key] = {"code": "invalid_value", "message": str(e)}
        return parsed, errors

    def _apply_values(self, cfg: Config, values: dict[str, Any], persist: bool) -> None:
        errors: dict[str, dict] = {}
        for key, value in values.items():
            spec = self.registry.specs.get(key)
            if spec is None:
                errors[key] = {"code": "unknown_key", "message": f"unknown setting {key!r}"}
                continue
            if spec.apply_mode == "installer_only":
                errors[key] = {"code": "installer_only", "message": "installer-only keys cannot be applied"}
                continue
            try:
                parsed = parse_value(spec, value)
                if parsed is RESET:
                    continue
                spec.setter(cfg, parsed)
            except ValueError as e:
                errors[key] = {"code": "invalid_value", "message": str(e)}
        proposed = {k: v for k, v in values.items() if v is not RESET}
        for validator in self.registry.validators:
            for item in validator(cfg, proposed):
                errors.setdefault(item["key"], {"code": item["code"], "message": item["message"]})
        if errors:
            raise SettingsError(400, "configuration is invalid", "validation_error", keys=errors)

    # --- app -------------------------------------------------------------
    def patch_app(self, app_id: str, key: dict, changes: dict[str, Any], revision: int | None,
                  dry_run: bool = False) -> dict[str, Any]:
        if key.get("kind") != "app":
            raise SettingsError(403, "only an app token can change app configuration", "forbidden")
        if key.get("id") != app_id:
            raise SettingsError(403, "an app cannot change another app's configuration", "forbidden")
        if not isinstance(changes, dict):
            raise SettingsError(400, "changes must be an object", "invalid_request")
        envelope = self._app_envelope(app_id)
        if revision is not None and revision != envelope.revision:
            self._audit("app", "revision_conflict", list(changes), "failure", revision=envelope.revision,
                        actor=key)
            raise SettingsError(
                409,
                f"configuration revision {revision} is stale; current revision is {envelope.revision}",
                "revision_conflict",
                details={"expected_revision": revision, "current_revision": envelope.revision},
            )
        scopes = set((key.get("scopes") or "").split()) | set(key.get("scope_set") or [])
        parsed: dict[str, Any] = {}
        errors: dict[str, dict] = {}
        for name, value in changes.items():
            spec = self.registry.specs.get(name)
            if spec is None or spec.scope != "app":
                errors[name] = {"code": "unknown_key", "message": f"unknown setting {name!r}"}
                continue
            if any(cap not in scopes for cap in spec.capabilities):
                errors[name] = {"code": "missing_capability",
                                "message": f"this token lacks {', '.join(spec.capabilities)}"}
                continue
            try:
                parsed[name] = parse_value(spec, value)
            except ValueError as e:
                errors[name] = {"code": "invalid_value", "message": str(e)}
            else:
                extra = self._app_policy_errors(spec, parsed[name], key)
                if extra:
                    errors[name] = extra
        if errors:
            self._audit("app", "validate", list(changes), "failure", actor=key, extra={"keys": errors})
            raise SettingsError(400, "configuration is invalid", "validation_error", keys=errors)
        plan_changes = []
        new_values = dict(envelope.values)
        for name, value in parsed.items():
            spec = self.registry.get(name)
            before = envelope.values.get(name, spec.default)
            if value is RESET:
                new_values.pop(name, None)
                after = spec.default
                action = "reset"
            else:
                new_values[name] = value
                after = value
                action = "set"
            plan_changes.append({"key": name, "from": before, "to": after if action == "set" else None,
                                 "action": action, "apply": "live"})
        if dry_run:
            return {"revision": envelope.revision, "target_revision": envelope.revision + 1,
                    "dry_run": True, "changes": plan_changes, "restart_required": False}
        envelope.revision += 1
        envelope.values = new_values
        envelope.updated_at = time.time()
        self._save_app_envelope(app_id, envelope)
        self._audit("app", "patch", list(parsed), "ok", revision=envelope.revision, actor=key)
        view = self.app_view(app_id, key)
        view["changes"] = plan_changes
        return view

    def _app_policy_errors(self, spec: SettingSpec, value: Any, key: dict) -> dict | None:
        if value is RESET or value in (None, "", []):
            return None
        if spec.key == "app.default_backend":
            if not self._backend_allowed(key, str(value)):
                return {"code": "dependency", "message": "this app cannot select that backend"}
        if spec.key == "app.default_effort" and value not in ("", *("low", "medium", "high")):
            return {"code": "invalid_value", "message": "effort must be inherit-empty, low, medium, or high"}
        if spec.key == "app.capabilities":
            allowed = self._allowed_app_capabilities(key)
            extra = [item for item in value if item not in allowed]
            if extra:
                return {"code": "dependency",
                        "message": f"capabilities {extra} are not granted, installed, and allowed"}
        if spec.key == "app.notify.completion" and value not in ("inherit", "never"):
            return {"code": "invalid_value", "message": "completion notifications must be inherit or never"}
        return None

    def delete_app(self, app_id: str) -> None:
        if self.db is None:
            return
        self.db.delete_app_settings(app_id)

    def app_defaults(self, key: dict | None) -> dict[str, Any]:
        if not key or key.get("kind") != "app":
            return {}
        envelope = self._app_envelope(key["id"])
        out = {}
        for spec in APP_SPECS:
            configured = envelope.values.get(spec.key, spec.default)
            effective, _ = self._effective_app_value(spec, configured, key)
            out[spec.key] = effective
        return out

    def session_budgets(self, app_key: dict | None) -> tuple[int, int]:
        turns = self.cfg.max_turns
        tokens = self.cfg.max_completion_tokens
        if app_key and app_key.get("kind") == "app":
            defaults = self.app_defaults(app_key)
            if defaults.get("app.sessions.max_turns") is not None:
                turns = int(defaults["app.sessions.max_turns"])
            if defaults.get("app.sessions.max_completion_tokens") is not None:
                tokens = int(defaults["app.sessions.max_completion_tokens"])
        return turns, tokens

    # --- persistence helpers --------------------------------------------
    def _active(self) -> Envelope | None:
        try:
            return self.store.read_active()
        except ManagedConfigError:
            return None

    def _pending(self) -> Envelope | None:
        try:
            return self.store.read_pending()
        except ManagedConfigError:
            return None

    def _app_envelope(self, app_id: str) -> AppEnvelope:
        if self.db is None:
            return AppEnvelope()
        row = self.db.get_app_settings(app_id)
        if not row:
            return AppEnvelope()
        return AppEnvelope(schema_version=row.get("schema_version", SCHEMA_VERSION),
                           revision=int(row.get("revision") or 0),
                           values=dict(row.get("values") or {}),
                           updated_at=float(row.get("updated_at") or 0))

    def _save_app_envelope(self, app_id: str, envelope: AppEnvelope) -> None:
        self.db.set_app_settings(app_id, envelope.revision, envelope.values)

    def _audit(self, actor_kind: str, action: str, keys: list[str], result: str, revision: int | None = None,
               actor: dict | None = None, extra: dict | None = None) -> None:
        record = {
            "ts": time.time(),
            "actor_kind": actor_kind,
            "actor_id": (actor or {}).get("id") or "",
            "action": action,
            "keys": [key for key in keys if not looks_hidden(key, self.registry.specs.get(key))],
            "result": result,
            "revision": revision,
        }
        if extra:
            safe = dict(extra)
            if "changes" in safe:
                safe["changes"] = [
                    {**row, "from": None if looks_hidden(row["key"], self.registry.specs.get(row["key"])) else row.get("from"),
                     "to": None if looks_hidden(row["key"], self.registry.specs.get(row["key"])) else row.get("to")}
                    for row in safe["changes"]
                ]
            record["extra"] = safe
        try:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
        except OSError:
            log.warning("could not write config audit log")


def _actor_kind(actor: dict | None) -> str:
    if not actor:
        return "owner"
    return str(actor.get("kind") or "owner")


def _clone(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _load_yaml_files(config_dir: Path | None) -> dict[str, dict]:
    if config_dir is None:
        return {}
    config_dir = Path(config_dir)
    files = {}
    mapping = {"base": "harness.yaml", "local": "harness.local.yaml", "profile": "profile.yaml"}
    for name, filename in mapping.items():
        path = config_dir / filename
        if not path.is_file():
            files[name] = {}
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        files[name] = data if isinstance(data, dict) else {}
    return files


def module_installed_or_effective(cfg: Config, name: str) -> bool:
    installed = getattr(cfg, "installed", None)
    if installed is not None and hasattr(installed, name):
        return bool(getattr(installed, name) or getattr(cfg.modules, name, False))
    return bool(getattr(cfg.modules, name, False))


def raise_as_harness(error: SettingsError):
    from .manager import HarnessError
    exc = HarnessError(error.status, str(error), error.code)
    exc.keys = error.keys
    exc.details = error.details
    return exc


def schedule_exit() -> None:
    """Ask this process to exit after the HTTP response is flushed. The supervisor restarts it."""
    def _die():
        time.sleep(0.4)
        os.kill(os.getpid(), getattr(signal, "SIGTERM", signal.SIGINT))
    threading.Thread(target=_die, name="supervised-restart", daemon=True).start()
