"""The nightly backup: the module's service (#334; docs/modules.md).

Online copy of the SQLite databases plus transcripts and project config into ``backup.dir/<date>``, then pruning of
dated folders older than ``backup.keep_days``. It was ``Maintenance``'s backup section; the cleanup sweep stays in
the core. ``backup.enabled`` schedules the nightly run; POST /maintenance/backup runs one on demand whenever the
module is present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from harness.modules import (APP_STORE_FILE, OWNER_USER_ID, ROOT, ManagedStore, app_dir, backup_sqlite,
                             remove_tree, storage)

from .member_key import WARNING, snapshot_key

log = logging.getLogger("harness.backup")


class BackupService:
    def __init__(self, cfg, db, participant=None):
        self.cfg = cfg
        self.db = db
        self.participant = participant or (lambda: None)   # the image archive joins the backup (images module)
        self._backup_task: asyncio.Task | None = None
        self.last_backup: dict = self._read_backup_status()

    def seed_participant(self) -> None:
        """Give the image archive the reconciliation the last backup recorded (it survives a restart)."""
        archive = self.participant()
        if archive and isinstance(self.last_backup.get("image_archive"), dict):
            archive.last_reconciliation = self.last_backup["image_archive"]

    def start(self) -> None:
        if self._backup_task is None and self.cfg.backup.enabled:
            self._backup_task = asyncio.create_task(self._backup_loop(), name="backup")

    def reschedule_backup(self) -> None:
        """Pick up a live backup.at / keep_days change on the next wait."""
        if self._backup_task is not None:
            self._backup_task.cancel()
            self._backup_task = None
        self.start()

    async def stop(self) -> None:
        if self._backup_task:
            self._backup_task.cancel()
            await asyncio.gather(self._backup_task, return_exceptions=True)
        self._backup_task = None

    def status(self) -> dict:
        """What /maintenance reports under ``backup``."""
        return {**self.last_backup, "enabled": self.cfg.backup.enabled, "dir": self.cfg.backup.dir}

    @property
    def _backup_status_file(self) -> Path:
        return self.cfg.data_dir / "backup-status.json"

    def _read_backup_status(self) -> dict:
        try:
            return json.loads(self._backup_status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def next_backup_at(self, now: float) -> float:
        hour, minute = (int(x) for x in self.cfg.backup.at.split(":"))
        t = time.localtime(now)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
        return target if target > now else time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, hour, minute, 0, 0, 0, -1))

    async def _backup_loop(self) -> None:
        # Catch up at start if the last good backup is more than a day old (the tower was off at backup time).
        if time.time() - self.last_backup.get("ok_at", 0) > 26 * 3600:
            await asyncio.sleep(300)
            await self._backup_logged()
        while True:
            await asyncio.sleep(max(1.0, self.next_backup_at(time.time()) - time.time()))
            await self._backup_logged()

    async def _backup_logged(self) -> None:
        try:
            await self.backup()
        except Exception as e:  # noqa: BLE001 - keep the loop alive; the failure is in the status and metrics
            log.exception("backup failed")
            self.last_backup = {**self.last_backup, "error": f"{type(e).__name__}: {e}", "error_at": time.time()}
            self._write_backup_status()

    def _write_backup_status(self) -> None:
        try:
            self._backup_status_file.write_text(json.dumps(self.last_backup, indent=2), encoding="utf-8")
        except OSError:
            log.warning("could not write %s", self._backup_status_file)

    async def backup(self) -> dict:
        """Online copy of the SQLite database plus transcripts and project config into backup.dir/<date>, then
        delete dated folders older than keep_days."""
        result = await asyncio.to_thread(self._backup_sync, time.time())
        archive = self.participant()
        if archive and archive.enabled:
            # Image failures are warnings: the already-verified SQLite snapshot remains a successful backup.
            try:
                result["image_archive"] = await asyncio.to_thread(archive.reconcile)
            except Exception as e:  # noqa: BLE001 - report archive health without invalidating the snapshot
                result["image_archive"] = {"enabled": True, "errors": 1, "warnings": [str(e)]}
        self.last_backup = result
        self._write_backup_status()
        log.info("backup written to %s (%d bytes)", result["path"], result["bytes"])
        return result

    def _backup_sync(self, now: float) -> dict:
        root = Path(self.cfg.backup.dir)
        dest = root / time.strftime("%Y-%m-%d", time.localtime(now))
        tmp = root / (dest.name + ".partial")
        remove_tree(tmp)
        tmp.mkdir(parents=True)
        db_copy = tmp / "harness.sqlite3"
        self._backup_db(db_copy)
        warnings: list[str] = []
        member_key = snapshot_key(self.cfg, root, db_copy, warnings)
        for warning in warnings:
            log.warning(warning)
        if member_key:
            log.warning(WARNING)
        app_stores = self._backup_app_stores(tmp / "apps")
        transcript_archives = self._archive_transcripts(
            storage.transcripts_dir(self.cfg, OWNER_USER_ID), tmp / "transcripts.zip", warnings, owner=True)
        transcript_archives += self._backup_other_transcripts(tmp / "transcripts", warnings)
        config = tmp / "config"
        config.mkdir()
        for name in ("harness.yaml", "harness.local.yaml", "projects.yaml"):
            if (ROOT / "config" / name).exists():
                shutil.copy2(ROOT / "config" / name, config / name)
        managed = ManagedStore(self.cfg.data_dir)
        for path in managed.overlay_files():
            shutil.copy2(path, tmp / path.name)
        remove_tree(dest)
        tmp.rename(dest)

        removed = self._prune_old_backups(root, dest, now - self.cfg.backup.keep_days * 86400)
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        return {"ok_at": now, "path": str(dest), "bytes": size, "removed": removed, "error": "",
                "app_stores": app_stores, "transcript_archives": transcript_archives, "warnings": warnings,
                "member_key_path": str(member_key) if member_key else ""}

    @staticmethod
    def _skip_transcript_link(path: Path, warnings: list[str]) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        if storage.is_reparse_point(path):
            warnings.append(f"Skipped transcript link: {path}")
            return True
        return False

    def _transcript_files(self, root: Path, warnings: list[str]):
        # Check ancestors within storage, but allow OS redirects above the configured data directory.
        data_root = Path(self.cfg.data_dir).absolute()
        absolute_root = root.absolute()
        for path in reversed((absolute_root, *absolute_root.parents)):
            if path.is_relative_to(data_root) and self._skip_transcript_link(path, warnings):
                return
        if not root.is_dir():
            return
        yield from self._walk_transcripts(root, warnings)

    def _walk_transcripts(self, folder: Path, warnings: list[str]):
        for path in sorted(folder.iterdir()):
            if self._skip_transcript_link(path, warnings):
                continue
            if path.is_dir():
                yield from self._walk_transcripts(path, warnings)
            elif path.is_file():
                yield path

    def _archive_transcripts(self, root: Path, dest: Path, warnings: list[str], *, owner=False) -> int:
        import zipfile

        try:
            files = iter(self._transcript_files(root, warnings))
            first = next(files, None)
            if first is None and not owner:
                return 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
                if first is not None:
                    archive.write(first, first.relative_to(root).as_posix())
                for path in files:
                    archive.write(path, path.relative_to(root).as_posix())
            return 1
        except Exception as e:  # noqa: BLE001 - secondary archives must not invalidate the database snapshot
            if owner:
                raise
            dest.unlink(missing_ok=True)
            warnings.append(f"Transcript archive failed for {root}: {type(e).__name__}: {e}")
            return 0

    def _backup_other_transcripts(self, dest: Path, warnings: list[str]) -> int:
        count = 0
        for account in self.db.list_accounts():
            user_id = account["user_id"]
            if user_id != OWNER_USER_ID:
                count += self._archive_transcripts(storage.transcripts_dir(self.cfg, user_id),
                                                   dest / "users" / f"{user_id}.zip", warnings)
        root = Path(self.cfg.data_dir) / "apps"
        if self._skip_transcript_link(root, warnings) or not root.is_dir():
            return count
        for folder in sorted(root.iterdir()):
            try:
                app_dir(self.cfg.data_dir, folder.name)
            except ValueError:
                continue
            count += self._archive_transcripts(storage.transcripts_dir(self.cfg, OWNER_USER_ID, folder.name),
                                               dest / "apps" / f"{folder.name}.zip", warnings)
        return count

    def _backup_db(self, db_copy: Path) -> None:
        backup_sqlite(self.cfg.db_path, db_copy)

    def _backup_app_stores(self, dest: Path) -> int:
        """Every App's store, one file per App: `apps/<app_id>.sqlite3` (#330 decision 6). Returns how many."""
        root, count = Path(self.cfg.data_dir) / "apps", 0
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            try:
                store = app_dir(self.cfg.data_dir, folder.name) / APP_STORE_FILE
            except ValueError:  # not an App's folder
                continue
            if store.is_file():
                dest.mkdir(exist_ok=True)
                backup_sqlite(store, dest / f"{folder.name}.sqlite3")
                count += 1
        return count

    @staticmethod
    def _prune_old_backups(root: Path, dest: Path, cutoff: float) -> list:
        removed = []
        for old in sorted(root.iterdir()):
            if old.is_dir() and old != dest and len(old.name) >= 10 and old.name[:4].isdigit():
                try:
                    stamp = time.mktime(time.strptime(old.name[:10], "%Y-%m-%d"))
                except ValueError:
                    continue
                if stamp < cutoff:
                    remove_tree(old)
                    removed.append(old.name)
        return removed
