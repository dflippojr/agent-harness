"""Sandbox cleanup and disk accounting.

Runs every `cleanup.interval_minutes` and on demand (POST /maintenance/cleanup):
- removes the stopped containers of finished sessions after `container_idle_hours` (a follow-up message creates a
  fresh one; anything installed inside the old container is gone), and containers whose session no longer exists;
- deletes the workspaces of finished sessions after `workspace_retention_days`. The branch of a local git project
  was already saved into the source repository at the end of each run, so only the checkout goes. A URL project
  with commits that were never pushed is kept;
- deletes workspace directories that belong to no session;
- asks runners (the MacBook) to delete their finished sessions' workspaces after the same retention, saving the
  branch into the Mac's source repository first. A runner that's offline is simply asked again next time.
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

from . import projects
from .config import Config
from .db import Database
from .remote import RunnerError
from .runner import ACTIVE, Runner, dir_size
from .sandbox import run_cmd

log = logging.getLogger("harness.maintenance")


def remove_tree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (git objects are read-only on Windows)."""
    def on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=on_error)


class Maintenance:
    def __init__(self, cfg: Config, db: Database, runner: Runner):
        self.cfg = cfg
        self.db = db
        self.runner = runner
        self._task: asyncio.Task | None = None
        self.last_report: dict = {}
        self.last_backup: dict = self._read_backup_status()
        self._lock = asyncio.Lock()
        self._backup_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None and self.cfg.cleanup.interval_minutes > 0:
            self._task = asyncio.create_task(self._loop(), name="maintenance")
        if self._backup_task is None and self.cfg.backup.enabled:
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
        self.last_backup = result
        self._write_backup_status()
        log.info("backup written to %s (%d bytes)", result["path"], result["bytes"])
        return result

    def _backup_sync(self, now: float) -> dict:
        import sqlite3
        import zipfile
        from .config import ROOT

        root = Path(self.cfg.backup.dir)
        dest = root / time.strftime("%Y-%m-%d", time.localtime(now))
        tmp = root / (dest.name + ".partial")
        remove_tree(tmp)
        tmp.mkdir(parents=True)
        db_copy = tmp / "harness.sqlite3"
        source = sqlite3.connect(str(self.cfg.db_path))
        target = sqlite3.connect(str(db_copy))
        try:
            source.backup(target)  # consistent snapshot while the daemon keeps writing (WAL)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
            source.close()
        if check != "ok":
            raise RuntimeError(f"backup copy failed its integrity check: {check}")
        with zipfile.ZipFile(tmp / "transcripts.zip", "w", zipfile.ZIP_DEFLATED) as z:
            if self.cfg.transcripts_dir.is_dir():
                for f in sorted(self.cfg.transcripts_dir.rglob("*")):
                    if f.is_file():
                        z.write(f, f.relative_to(self.cfg.transcripts_dir).as_posix())
        config = tmp / "config"
        config.mkdir()
        for name in ("harness.yaml", "harness.local.yaml", "projects.yaml"):
            if (ROOT / "config" / name).exists():
                shutil.copy2(ROOT / "config" / name, config / name)
        remove_tree(dest)
        tmp.rename(dest)

        removed = []
        cutoff = now - self.cfg.backup.keep_days * 86400
        for old in sorted(root.iterdir()):
            if old.is_dir() and old != dest and len(old.name) >= 10 and old.name[:4].isdigit():
                try:
                    stamp = time.mktime(time.strptime(old.name[:10], "%Y-%m-%d"))
                except ValueError:
                    continue
                if stamp < cutoff:
                    remove_tree(old)
                    removed.append(old.name)
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        return {"ok_at": now, "path": str(dest), "bytes": size, "removed": removed, "error": ""}

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
                      "kept": []}
            await self._containers(now, report)
            await asyncio.to_thread(self._workspaces, now, report)
            await self._remote_workspaces(now, report)
            self.last_report = report
            if any(report[k] for k in ("containers_removed", "workspaces_removed", "orphans_removed")):
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
            labels = dict(kv.split("=", 1) for kv in item.get("Labels", "").split(",") if "=" in kv)
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
        from .storage import workspaces_dir
        roots = [self.cfg.workspaces_dir]
        users = self.cfg.data_dir / "users"
        if users.is_dir():
            for child in users.iterdir():
                if child.is_dir() and not child.is_symlink():
                    roots.append(child / "workspaces")
        return roots

    def _workspaces(self, now: float, report: dict) -> None:
        retention = self.cfg.cleanup.workspace_retention_days * 86400
        for root in self._workspace_roots():
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_dir():
                    continue
                s = self.db.get_session(path.name)
                if s is None:
                    if now - path.stat().st_mtime > 3600:  # never race a session being created
                        remove_tree(path)
                        report["orphans_removed"].append(path.name)
                    continue
                if s["status"] in ACTIVE or s["workspace_removed"] or now - s["updated_at"] < retention:
                    continue
                reason = self.unsaved_work(s)
                if reason:
                    report["kept"].append({"session": s["id"], "reason": reason})
                    continue
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
            from . import catalog
            from .principal import session_user_id
            project = catalog.get_project(self.cfg, self.db, session_user_id(s), s.get("project") or "")
            try:
                result = await hub.call(target, "cleanup_workspace", {
                    "session": s["id"], "repo": project.repo if project else "", "branch": s["branch"],
                    "base_commit": s["base_commit"], "review": s["review"]}, timeout=300, wait_if_offline=False)
            except RunnerError as e:
                report["kept"].append({"session": s["id"], "reason": str(e)})
                continue
            except Exception as e:  # noqa: BLE001 - offline between the check and the call
                report["kept"].append({"session": s["id"], "reason": f"{type(e).__name__}: {e}"})
                continue
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
        s = self.db.get_session(sid)
        remove_tree(Path(s["workspace"]))
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
            disk = shutil.disk_usage(self.cfg.data_dir)
            return {"workspaces": sorted(sizes, key=lambda x: -x["mb"]),
                    "workspaces_mb": round(sum(x["mb"] for x in sizes), 1),
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
        return out
