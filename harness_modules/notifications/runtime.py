"""Notifications inside a running daemon: the ModuleRuntime the core drives (harness/modules.py)."""

from __future__ import annotations

from harness.modules import ModuleRuntime

from .service import Notifier


class NotificationsRuntime(ModuleRuntime):
    """The Notifier exists whenever the module is present; it queues nothing while ``notify.enabled`` is off."""

    def __init__(self, manager, module):
        super().__init__(manager, module)
        # The host is built after this runtime: the lookup resolves it when a job's run finishes.
        self.service = Notifier(manager.cfg, manager.db, scheduled_job=lambda jid: manager.modules.scheduled_job(jid))
        manager.bus.add_listener(self.service.listener)

    def status(self) -> dict:
        return {"enabled": self.service.enabled, "queued": self.service.queue.qsize()}

    def start(self) -> None:
        self.service.start()

    async def stop(self) -> None:
        await self.service.stop()
