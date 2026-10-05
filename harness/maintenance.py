"""Sandbox cleanup and disk accounting.

Runs every `cleanup.interval_minutes` and on demand (POST /maintenance/cleanup):
- removes the stopped containers of finished sessions after `container_idle_hours` (a follow-up message creates a
  fresh one; anything installed inside the old container is gone), and containers whose session no longer exists;
- deletes the workspaces of finished sessions after `workspace_retention_days`. The branch of a local git project
  was already saved into the source repository at the end of each run, so only the checkout goes. A URL project
  with commits that were never pushed is kept;
- deletes workspace directories that belong to no session;
- asks runners (the MacBook) to delete their finished sessions' workspaces after the same retention, saving the
  branch into the Mac's source repository first. A runner that's offline is simply asked again next time;
- erases App data (#330 decision 5, `app_sweep`, the manager's `sweep_app_data`): the store and folder of a revoked
  App once its 7-day grace is over, and App sessions past their retention, whether or not the App is online.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path

from . import projects, storage
from .config import Config
from .db import Database
from .principal import OWNER_USER_ID
from .storage import checkpoints_dir
from .remote import RunnerError
from .runner import ACTIVE, Runner, dir_size
from .sandbox import run_cmd
from .sqlite_backup import backup_sqlite

log = logging.getLogger("harness.maintenance")


def remove_tree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (git objects are read-only on Windows)."""
    def on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=on_error)


class Maintenance:
    def __init__(self, cfg: Config, db: Database, runner: Runner, image_archive=None):
        self.cfg = cfg
        self.db = db
        self.runner = runner
        self.image_archive = image_archive
        self._task: asyncio.Task | None = None
        self.last_report: dict = {}
        self.operations: dict[str, str] = {}    # the manager's sessions held by a rewind, fork or review
        self.app_sweep = None                    # async (now) -> report: erases expired and revoked App data
        self.last_backup: dict = self._read_backup_status()
        if self.image_archive and isinstance(self.last_backup.get("image_archive"), dict):
            self.image_archive.last_reconciliation = self.last_backup["image_archive"]
        self._lock = asyncio.Lock()
        self._backup_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None and self.cfg.cleanup.interval_minutes > 0:
            self._task = asyncio.create_task(self._loop(), name="maintenance")
        if self._backup_task is None and self.cfg.backup.enabled:
            self._backup_task = asyncio.create_task(self._backup_loop(), name="backup")

    def reschedule(self) -> None:
        """Apply a live change to cleanup.interval_minutes by restarting the sweep task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.cfg.cleanup.interval_minutes > 0:
            self._task = asyncio.create_task(self._loop(), name="maintenance")

    def reschedule_backup(self) -> None:
        """Pick up a live backup.at / keep_days change on the next wait."""
        if self._backup_task is not None:
            self._backup_task.cancel()
            self._backup_task = None
        if self.cfg.backup.enabled:
            self._backup_task = asyncio.create_task(self._backup_loop(), name="backup")

    async def stop(self) -> None:
        for task in (self._task, self._backup_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = self._backup_task = None

    # backups
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
        if self.image_archive and self.image_archive.enabled:
            # Image failures are warnings: the already-verified SQLite snapshot remains a successful backup.
            try:
                result["image_archive"] = await asyncio.to_thread(self.image_archive.reconcile)
            except Exception as e:  # noqa: BLE001 - report archive health without invalidating the snapshot
                result["image_archive"] = {"enabled": True, "errors": 1, "warnings": [str(e)]}
        self.last_backup = result
        self._write_backup_status()
        log.info("backup written to %s (%d bytes)", result["path"], result["bytes"])
        return result

    def _backup_sync(self, now: float) -> dict:
        from .config import ROOT

        root = Path(self.cfg.backup.dir)
        dest = root / time.strftime("%Y-%m-%d", time.localtime(now))
        tmp = root / (dest.name + ".partial")
        remove_tree(tmp)
        tmp.mkdir(parents=True)
        db_copy = tmp / "harness.sqlite3"
        self._backup_db(db_copy)
        app_stores = self._backup_app_stores(tmp / "apps")
        warnings: list[str] = []
        transcript_archives = self._archive_transcripts(
            storage.transcripts_dir(self.cfg, OWNER_USER_ID), tmp / "transcripts.zip", warnings, owner=True)
        transcript_archives += self._backup_other_transcripts(tmp / "transcripts", warnings)
        config = tmp / "config"
        config.mkdir()
        for name in ("harness.yaml", "harness.local.yaml", "projects.yaml"):
            if (ROOT / "config" / name).exists():
                shutil.copy2(ROOT / "config" / name, config / name)
        from .managed_config import ManagedStore
        managed = ManagedStore(self.cfg.data_dir)
        for path in managed.overlay_files():
            shutil.copy2(path, tmp / path.name)
        remove_tree(dest)
        tmp.rename(dest)

        removed = self._prune_old_backups(root, dest, now - self.cfg.backup.keep_days * 86400)
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        return {"ok_at": now, "path": str(dest), "bytes": size, "removed": removed, "error": "",
                "app_stores": app_stores, "transcript_archives": transcript_archives, "warnings": warnings}

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
        from .app_stores import app_dir

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
        from .app_stores import APP_STORE_FILE, app_dir
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

    async def _loop(self) -> None:
        await asyncio.sleep(120)  # let resumed sessions settle after a restart
        while True:
            try:
                await self.cleanup()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("cleanup failed")
            await asyncio.sleep(self.cfg.cleanup.interval_minutes * 60)

    # cleanup
    async def cleanup(self, now: float | None = None) -> dict:
        async with self._lock:
            now = now or time.time()
            report = {"at": now, "containers_removed": [], "workspaces_removed": [], "orphans_removed": [],
                      "kept": [], "apps_erased": [], "sessions_expired": []}
            if self.app_sweep is not None:  # first, so the sweep below removes the containers of what it erased
                try:
                    report.update(await self.app_sweep(now))
                except Exception:  # noqa: BLE001 - the rest of the cleanup still runs
                    log.exception("App data sweep failed")
            await self._containers(now, report)
            await asyncio.to_thread(self._workspaces, now, report)
            await self._remote_workspaces(now, report)
            self.last_report = report
            if any(report[k] for k in ("containers_removed", "workspaces_removed", "orphans_removed",
                                       "apps_erased", "sessions_expired")):
                log.info("cleanup: %s", {k: v for k, v in report.items() if k != "at"})
            return report

    async def _containers(self, now: float, report: dict) -> None:
        code, out, _ = await run_cmd(["docker", "ps", "-a", "--filter", "label=agent-harness.session",
                                      "--format", "{{json .}}"], timeout=60)
        if code != 0:
            return
        idle = self.cfg.cleanup.container_idle_hours * 3600
        for line in out.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            labels = {k: v for k, _, v in (kv.partition("=") for kv in item.get("Labels", "").split(",") if "=" in kv)}
            sid = labels.get("agent-harness.session", "")
            s = self.db.get_session(sid) if sid else None
            if item.get("State") == "running" and s is not None:
                continue
            if s is not None and (s["status"] in ACTIVE or now - s["updated_at"] < idle):
                continue
            await run_cmd(["docker", "rm", "-f", item["Names"]], timeout=60)
            self.runner._sandboxes.pop(sid, None)
            report["containers_removed"].append(item["Names"])

    def _workspace_roots(self) -> list:
        from .storage import is_reparse_point, workspaces_dir
        roots = [self.cfg.workspaces_dir]
        for parent in (self.cfg.data_dir / "users", self.cfg.data_dir / "apps"):  # members' and Apps' (#330)
            if parent.is_dir() and not is_reparse_point(parent):
                for child in parent.iterdir():
                    if child.is_dir() and not is_reparse_point(child):
                        roots.append(child / "workspaces")
        return roots

    def _workspaces(self, now: float, report: dict) -> None:
        from .storage import is_reparse_point
        retention = self.cfg.cleanup.workspace_retention_days * 86400
        for root in self._workspace_roots():
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_dir() or is_reparse_point(path):
                    continue
                self._clean_workspace(path, now, retention, report)

    def _clean_workspace(self, path, now: float, retention: float, report: dict) -> None:
        s = self.db.get_session(path.name)
        if s is None:
            if now - path.stat().st_mtime > 3600:  # never race a session being created
                remove_tree(path)
                report["orphans_removed"].append(path.name)
            return
        if (s["status"] in ACTIVE or s["id"] in self.operations or s["workspace_removed"]
                or now - s["updated_at"] < retention):
            return
        reason = self.unsaved_work(s)
        if reason:
            report["kept"].append({"session": s["id"], "reason": reason})
            return
        self.remove_workspace(s["id"])
        report["workspaces_removed"].append(s["id"])

    async def _remote_workspaces(self, now: float, report: dict) -> None:
        retention = self.cfg.cleanup.workspace_retention_days * 86400
        hub = self.runner.hub
        for s in self.db.sessions_with_status("done", "failed", "cancelled"):
            target = s["target"]
            if target == "tower" or s["workspace_removed"] or now - s["updated_at"] < retention:
                continue
            if not hub.online(target):
                continue
            await self._remote_cleanup_one(hub, s, report)

    async def _remote_cleanup_one(self, hub, s: dict, report: dict) -> None:
        from . import catalog
        from .principal import session_user_id
        project = catalog.get_project(self.cfg, self.db, session_user_id(s), s.get("project") or "")
        try:
            result = await hub.call(s["target"], "cleanup_workspace", {
                "session": s["id"], "repo": project.repo if project else "", "branch": s["branch"],
                "base_commit": s["base_commit"], "review": s["review"]}, timeout=300, wait_if_offline=False)
        except RunnerError as e:
            report["kept"].append({"session": s["id"], "reason": str(e)})
            return
        except Exception as e:  # noqa: BLE001 - offline between the check and the call
            report["kept"].append({"session": s["id"], "reason": f"{type(e).__name__}: {e}"})
            return
        if result.get("removed"):
            self.db.update_session(s["id"], workspace_removed=1)
            report["workspaces_removed"].append(s["id"])
        else:
            report["kept"].append({"session": s["id"], "reason": result.get("reason", "kept by the runner")})

    def unsaved_work(self, s: dict) -> str:
        """Why deleting this workspace would lose work, or ''."""
        from . import catalog
        from .principal import session_user_id
        project = catalog.get_project(self.cfg, self.db, session_user_id(s), s.get("project") or "")
        ws = Path(s["workspace"])
        if not project or not project.repo or not s["base_commit"] or not (ws / ".git").exists():
            return ""
        if projects.source_path(project) is not None:
            try:  # local source: make sure the branch there is current before the checkout goes
                projects.snapshot(ws, f"Uncommitted work before cleanup (session {s['id']})")
                projects.publish_local(project, ws, s["branch"])
            except projects.GitError as e:
                return f"could not save the branch: {e}"
            return ""
        if s["review"] in ("pushed", "discarded"):
            unpushed = projects.git(ws, "log", "--oneline", f"origin/{s['branch']}..HEAD", check=False)
            return "" if unpushed.code == 0 and not unpushed.out.strip() else "branch has commits that weren't pushed"
        return "" if not projects.commits_ahead(ws, s["base_commit"]) else "branch was never pushed"

    def remove_workspace(self, sid: str) -> None:
        from . import storage
        s = self.db.get_session(sid)
        path = Path(s["workspace"])
        dirs = storage.session_dirs(self.cfg, s)
        root = dirs["workspaces"]
        try:
            storage.require_contained(path, root)
        except storage.ContainmentError:
            log.warning("refusing to delete workspace for %s: path escapes the account root", sid)
            return
        remove_tree(path)
        remove_tree(dirs["checkpoints"] / sid)
        self.db.delete_checkpoints(sid, [c["turn"] for c in self.db.checkpoints(sid, hidden=None)])
        self.db.update_session(sid, workspace_removed=1)

    # reporting
    async def usage(self) -> dict:
        def measure() -> dict:
            root = self.cfg.workspaces_dir
            sizes = []
            if root.is_dir():
                for path in root.iterdir():
                    if path.is_dir():
                        sizes.append({"session": path.name, "mb": round(dir_size(path) / 2**20, 1)})
            ckpt_root = checkpoints_dir(self.cfg, OWNER_USER_ID)
            checkpoints = []
            if ckpt_root.is_dir():
                for path in ckpt_root.iterdir():
                    if path.is_dir():
                        checkpoints.append({"session": path.name, "mb": round(dir_size(path) / 2**20, 1)})
            disk = shutil.disk_usage(self.cfg.data_dir)
            return {"workspaces": sorted(sizes, key=lambda x: -x["mb"]),
                    "workspaces_mb": round(sum(x["mb"] for x in sizes), 1),
                    "checkpoints": sorted(checkpoints, key=lambda x: -x["mb"]),
                    "checkpoints_mb": round(sum(x["mb"] for x in checkpoints), 1),
                    "free_gb": round(disk.free / 2**30, 1), "total_gb": round(disk.total / 2**30, 1)}

        out = await asyncio.to_thread(measure)
        code, text, _ = await run_cmd(["docker", "ps", "-a", "-s", "--filter", "label=agent-harness.session",
                                       "--format", "{{.Names}}\t{{.State}}\t{{.Size}}"], timeout=120)
        out["containers"] = [dict(zip(("name", "state", "size"), line.split("\t")))
                             for line in text.splitlines()] if code == 0 else []
        out["quota_mb"] = self.cfg.cleanup.workspace_quota_mb
        out["runners"] = self.runner.hub.status()
        out["last_cleanup"] = self.last_report
        out["backup"] = {**self.last_backup, "enabled": self.cfg.backup.enabled, "dir": self.cfg.backup.dir}
        out["image_archive"] = self.image_archive.health() if self.image_archive else {"enabled": False}
        return out
