"""Sandbox cleanup and disk accounting.

Runs every `cleanup.interval_minutes` and on demand (POST /maintenance/cleanup):
- removes the stopped containers of finished sessions after `container_idle_hours` (a follow-up message creates a
  fresh one; anything installed inside the old container is gone), and containers whose session no longer exists;
- deletes the workspaces of finished sessions after `workspace_retention_days`. The branch of a local git project
  was already saved into the source repository at the end of each run, so only the checkout goes. A URL project
  with commits that were never pushed is kept;
- deletes workspace directories that belong to no session.
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
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None and self.cfg.cleanup.interval_minutes > 0:
            self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

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

    def _workspaces(self, now: float, report: dict) -> None:
        retention = self.cfg.cleanup.workspace_retention_days * 86400
        root = self.cfg.workspaces_dir
        if not root.is_dir():
            return
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

    def unsaved_work(self, s: dict) -> str:
        """Why deleting this workspace would lose work, or ''."""
        project = self.cfg.projects.get(s["project"])
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
        out["last_cleanup"] = self.last_report
        return out
