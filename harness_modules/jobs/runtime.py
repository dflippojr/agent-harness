"""The jobs scheduler lifecycle belongs to the add-on."""
from harness.modules import ModuleRuntime
from .service import JobScheduler


class JobsRuntime(ModuleRuntime):
    def init(self):
        if self.effective():
            self.service = JobScheduler(self.manager.db, self.manager.create,
                                        active=self.manager._is_active, poll_seconds=self.cfg.jobs.poll_seconds)

    def start(self):
        if self.service is not None:
            self.service.start()

    async def stop(self):
        if self.service is not None:
            await self.service.stop()
