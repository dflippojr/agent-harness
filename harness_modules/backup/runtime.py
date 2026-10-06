"""The backup inside a running daemon: the ModuleRuntime the core drives (harness/modules.py)."""

from __future__ import annotations

from harness.modules import ModuleRuntime

from .service import BackupService


class BackupRuntime(ModuleRuntime):
    """The service exists whenever the module is present (a manual backup works); ``backup.enabled`` schedules it."""

    def __init__(self, manager, module):
        super().__init__(manager, module)
        self.service = BackupService(manager.cfg, manager.db, lambda: manager.modules.backup_participant())

    def init(self) -> None:
        self.manager.maintenance.status_extras["backup"] = self.service.status
        self.service.seed_participant()

    def start(self) -> None:
        self.service.start()

    async def stop(self) -> None:
        await self.service.stop()

    def metrics(self, out, db) -> None:
        backup = self.service.last_backup
        if backup.get("ok_at"):
            out.metric("harness_backup_last_success_timestamp_seconds", "gauge", "Last successful backup.",
                       [({}, backup["ok_at"])])
            out.metric("harness_backup_size_bytes", "gauge", "Size of the last backup.",
                       [({}, backup.get("bytes", 0))])


def doctor(report, cfg) -> None:
    """Called by `python -m harness.doctor` once the daemon is known to answer."""
    import httpx
    try:
        backup = httpx.get(f"http://127.0.0.1:{cfg.port}/maintenance", timeout=60).json().get("backup") or {}
    except (httpx.HTTPError, ValueError):
        return
    if backup.get("enabled"):
        (report.ok if backup.get("ok_at") else report.warn)(
            "Backups", backup.get("path") or "no backup yet (the first runs 5 minutes after start)")
