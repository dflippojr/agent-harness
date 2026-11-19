"""The jobs scheduler lifecycle belongs to the add-on."""
from harness.modules import ModuleRuntime
from .service import JobScheduler
from .store import JobStore


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

    def scheduled_job(self, jid: str) -> dict | None:
        """Read whether or not scheduling is on: a run that already happened still names its job (notifications)."""
        return JobStore(self.manager.db).get_job(jid)
