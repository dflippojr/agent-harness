"""Crash-safe managed configuration overlay (issue #66).

managed-config.json holds only registered admin keys in a flat versioned envelope.
pending.json holds a restart-required candidate. lkg.json is the last confirmed good
generation. Files never contain secrets or nested YAML.

Overlay state machine (disk). Transitions go through ``overlay.next_overlay``;
the only merge is ``effective_candidate`` (active overlaid by pending). Boot
applies **active** only; pending is never loaded until an owner-confirmed
restart copies it onto active.

- **empty** — no active file. YAML/inherited values are effective. No LKG skip,
  because there is nothing to confirm.
- **confirmed** — ``managed-config.json`` with ``confirmed: true``. Effective
  generation: live keys plus the last *promoted* ``daemon_restart`` keys.
  A PATCH of a restart key must **not** drop or replace that key here; the
  candidate value lives only in pending until restart.
- **pending** — ``managed-config.pending.json`` holds the next generation
  (live + restart). Not applied at boot.
- **restarting** — owner confirmed ``POST /config/restart``: previous confirmed
  snapshot is LKG, pending is copied to active with ``confirmed: false``.
  ``boot-tried`` is clear so the next process may try the candidate once.
- **boot-tried** — this process marked the unconfirmed active generation as
  attempted. If it dies before ``confirm_startup``, the next process restores LKG.
- **confirmed-after-boot** — ``confirm_startup`` after ``/health``: active
  ``confirmed: true``, pending and boot-tried cleared.
- **quarantined** — unconfirmed/invalid candidate moved aside; LKG restored as
  confirmed active (or active unlinked if there is no LKG). If LKG is also
  unusable, active and LKG are timestamp-renamed and YAML defaults take effect.

Every generation change goes through ``ManagedStore.commit``. Writes use a temp
file, flush/fsync, then ``os.replace``. Multi-file order is quarantine → LKG →
pending → boot-tried → status → **active** (the single commit point). A crash
before replacing active leaves the previous confirmed generation in effect.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
ACTIVE_NAME = "managed-config.json"
PENDING_NAME = "managed-config.pending.json"
LKG_NAME = "managed-config.lkg.json"
QUARANTINE_NAME = "managed-config.quarantine.json"
LOCK_NAME = "managed-config.lock"
BOOT_TRIED_NAME = "managed-config.boot-tried"
STATUS_NAME = "managed-config.status.json"
UNSET = object()


class ManagedConfigError(ValueError):
    """The overlay is missing, corrupt, or not a valid envelope."""


class OverlayCrash(RuntimeError):
    """Test hook: abort ``commit`` after a named step. Not used in production."""

    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


@dataclass
class Envelope:
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    confirmed: bool = True
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    migrated_backend_prefs: bool = False
    previous_revision: int | None = None
    unconfirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "confirmed": self.confirmed and not self.unconfirmed,
            "values": dict(self.values),
            "updated_at": self.updated_at,
            "migrated_backend_prefs": self.migrated_backend_prefs,
            "previous_revision": self.previous_revision,
        }


def parse_envelope(data: Any) -> Envelope:
    if not isinstance(data, dict):
        raise ManagedConfigError("managed configuration must be an object")
    if "schema_version" not in data or "revision" not in data or "values" not in data:
        raise ManagedConfigError("managed configuration is missing schema_version, revision, or values")
    try:
        schema_version = int(data["schema_version"])
        revision = int(data["revision"])
    except (TypeError, ValueError) as e:
        raise ManagedConfigError("schema_version and revision must be integers") from e
    if schema_version != SCHEMA_VERSION:
        raise ManagedConfigError(f"unsupported managed-config schema_version {schema_version}")
    if revision < 0:
        raise ManagedConfigError("revision must be >= 0")
    values = data.get("values")
    if not isinstance(values, dict) or any(not isinstance(key, str) for key in values):
        raise ManagedConfigError("values must be a flat object keyed by strings")
    if any(isinstance(value, dict) for value in values.values()):
        raise ManagedConfigError("managed configuration cannot contain nested objects")
    previous = data.get("previous_revision")
    if previous is not None:
        try:
            previous = int(previous)
        except (TypeError, ValueError) as e:
            raise ManagedConfigError("previous_revision must be an integer") from e
    return Envelope(
        schema_version=schema_version,
        revision=revision,
        confirmed=bool(data.get("confirmed", True)),
        values=dict(values),
        updated_at=float(data.get("updated_at") or 0),
        migrated_backend_prefs=bool(data.get("migrated_backend_prefs", False)),
        previous_revision=previous,
        unconfirmed=not bool(data.get("confirmed", True)),
    )


class ManagedStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.active_path = self.data_dir / ACTIVE_NAME
        self.pending_path = self.data_dir / PENDING_NAME
        self.lkg_path = self.data_dir / LKG_NAME
        self.quarantine_path = self.data_dir / QUARANTINE_NAME
        self.lock_path = self.data_dir / LOCK_NAME
        self.boot_tried_path = self.data_dir / BOOT_TRIED_NAME
        self.status_path = self.data_dir / STATUS_NAME
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle = None
        self.crash_at: str | None = None  # test-only: OverlayCrash after this commit step

    def exists(self) -> bool:
        return self.active_path.is_file()

    def pending_exists(self) -> bool:
        return self.pending_path.is_file()

    def lkg_exists(self) -> bool:
        return self.lkg_path.is_file()

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            if self._lock_depth == 0:
                handle = open(self.lock_path, "a+b")
                try:
                    _acquire(handle)
                except Exception:
                    handle.close()
                    raise
                self._lock_handle = handle
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_handle is not None:
                    _release(self._lock_handle)
                    self._lock_handle.close()
                    self._lock_handle = None

    def read_active(self) -> Envelope | None:
        return self._read(self.active_path)

    def read_pending(self) -> Envelope | None:
        return self._read(self.pending_path)

    def read_lkg(self) -> Envelope | None:
        return self._read(self.lkg_path)

    def read_status(self) -> dict[str, Any]:
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_status(self, status: dict[str, Any]) -> None:
        self._write(self.status_path, status)

    def write_active(self, envelope: Envelope) -> None:
        envelope.updated_at = envelope.updated_at or time.time()
        self._write(self.active_path, envelope.to_dict())

    def write_pending(self, envelope: Envelope) -> None:
        envelope.updated_at = envelope.updated_at or time.time()
        self._write(self.pending_path, envelope.to_dict())

    def write_lkg(self, envelope: Envelope) -> None:
        envelope.updated_at = envelope.updated_at or time.time()
        envelope.confirmed = True
        envelope.unconfirmed = False
        self._write(self.lkg_path, envelope.to_dict())

    def clear_pending(self) -> None:
        self._unlink(self.pending_path)

    def mark_boot_tried(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.boot_tried_path.write_text("1", encoding="utf-8")
        _restrict(self.boot_tried_path)

    def boot_tried(self) -> bool:
        return self.boot_tried_path.is_file()

    def clear_boot_tried(self) -> None:
        self._unlink(self.boot_tried_path)

    def quarantine(self, envelope: Envelope, reason: str) -> None:
        payload = {**envelope.to_dict(), "reason": reason, "quarantined_at": time.time()}
        self._write(self.quarantine_path, payload)
        self.write_status({
            "recovery": "lkg_restore",
            "reason": reason,
            "quarantined_revision": envelope.revision,
            "at": time.time(),
        })

    def quarantine_managed_files(self, reason: str, warning: str | None = None) -> list[Path]:
        """Rename active, pending, and LKG aside with a timestamp and boot without them.

        Original bytes are kept for the owner. Canonical overlay paths become empty
        so the next start uses YAML defaults.
        """
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        kept: list[Path] = []
        for path in (self.active_path, self.pending_path, self.lkg_path):
            if not path.is_file():
                continue
            dest = path.with_name(f"{path.name}.quarantine.{stamp}")
            suffix = 1
            while dest.exists():
                dest = path.with_name(f"{path.name}.quarantine.{stamp}.{suffix}")
                suffix += 1
            try:
                os.replace(path, dest)
                kept.append(dest)
            except OSError:
                try:
                    dest.write_bytes(path.read_bytes())
                    path.unlink()
                    kept.append(dest)
                except OSError:
                    continue
            _restrict(dest)
        self.clear_boot_tried()
        self.write_status({
            "recovery": "overlay_quarantined",
            "reason": reason,
            "warning": warning or reason,
            "quarantined": [path.name for path in kept],
            "at": time.time(),
        })
        return kept

    def commit(self, *, active=UNSET, pending=UNSET, lkg=UNSET, unlink_active: bool = False,
               boot_tried=UNSET, status=UNSET, quarantine_envelope: Envelope | None = None,
               quarantine_reason: str | None = None, quarantine_raw: bool = False) -> None:
        """Publish one overlay generation. Replacing active is the commit point."""
        if quarantine_envelope is not None:
            self.quarantine(quarantine_envelope, quarantine_reason or "")
            self._crash("quarantine")
        elif quarantine_raw and self.active_path.is_file():
            try:
                self.quarantine_path.write_text(self.active_path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
            self.write_status({"recovery": "lkg_restore", "reason": quarantine_reason or "", "at": time.time()})
            self._crash("quarantine")
        if lkg is not UNSET and lkg is not None:
            self.write_lkg(lkg)
            self._crash("lkg")
        if pending is not UNSET:
            if pending is None:
                self.clear_pending()
            else:
                self.write_pending(pending)
            self._crash("pending")
        if boot_tried is not UNSET:
            if boot_tried:
                self.mark_boot_tried()
            else:
                self.clear_boot_tried()
            self._crash("boot_tried")
        if status is not UNSET and status is not None:
            self.write_status(status)
            self._crash("status")
        if unlink_active:
            self._unlink(self.active_path)
            self._crash("active")
        elif active is not UNSET and active is not None:
            self.write_active(active)
            self._crash("active")

    def _crash(self, step: str) -> None:
        if self.crash_at == step:
            raise OverlayCrash(step)

    def restore_lkg(self, reason: str) -> Envelope | None:
        lkg = None
        try:
            lkg = self.read_lkg()
        except ManagedConfigError:
            lkg = None
        try:
            failed = self.read_active()
        except ManagedConfigError:
            failed = None
        if lkg is None:
            self.commit(
                quarantine_envelope=failed,
                quarantine_reason=reason,
                quarantine_raw=failed is None,
                unlink_active=True,
                pending=None,
                boot_tried=False,
            )
            return None
        lkg.confirmed = True
        lkg.unconfirmed = False
        self.commit(
            quarantine_envelope=failed,
            quarantine_reason=reason,
            quarantine_raw=failed is None,
            active=lkg,
            pending=None,
            boot_tried=False,
        )
        return lkg

    def overlay_files(self) -> list[Path]:
        return [path for path in (self.active_path, self.pending_path, self.lkg_path, self.quarantine_path)
                if path.is_file()]

    def _read(self, path: Path) -> Envelope | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ManagedConfigError(f"{path.name} is not valid JSON") from e
        return parse_envelope(data)

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(self.data_dir))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            _restrict(tmp)
            os.replace(tmp, path)
            _restrict(path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _acquire(handle) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt
        handle.write(b"\0")
        handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
