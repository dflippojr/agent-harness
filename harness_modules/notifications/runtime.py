"""Notifications inside a running daemon: the ModuleRuntime the core drives (harness/modules.py)."""

from __future__ import annotations

from harness.modules import ModuleRuntime

from .service import Notifier


class NotificationsRuntime(ModuleRuntime):
    """The Notifier exists whenever the module is present; it queues nothing while ``notify.enabled`` is off."""

    def __init__(self, manager, module):
        super().__init__(manager, module)
        self.service = Notifier(manager.cfg, manager.db)
        manager.bus.add_listener(self.service.listener)

    def start(self) -> None:
        self.service.start()

    async def stop(self) -> None:
        await self.service.stop()
