"""Supervision lifecycle and wiring, reached only through the module host."""
import asyncio

from harness.modules import ModuleRuntime, ACTIVE
from .service import GpuGuard, ServerControl
from .warmup import ModelWarmer


class LocalModelRuntime(ModuleRuntime):
    def init(self):
        m, cfg = self.manager, self.cfg
        self.service = ModelWarmer()
        m.warmer = m.runner.warmer = self.service
        if self.effective("gpu_guard"):
            m.guard = GpuGuard(cfg.gpu_guard, cfg.models[cfg.default_model], m.scheduler,
                               busy=lambda: bool(m.runner.generating) or m.runner.gate.busy
                               or m.runner.gate.exclusive,
                               on_pause=self._gpu_paused, on_resume=self._gpu_resumed,
                               data_dir=cfg.data_dir)
            m.runner.guard = m.guard
            self.service.blocked = lambda: m.guard.active or m.guard.manual or m.modules.gpu_taken
        else:
            self.service.blocked = lambda: m.modules.gpu_taken

    def model_control(self):
        return ServerControl(self.cfg.gpu_guard, self.cfg.models[self.cfg.default_model])

    def after_init(self):
        m, cfg = self.manager, self.cfg
        guard, warmer = m.guard, self.service
        if guard is None:
            return
        warmer.control = lambda: guard.control
        warmer.managed_model = cfg.models[cfg.default_model].name
        warmer.memory_low = lambda: guard.memory.load_low()
        warmer.read_available = guard.memory.available
        warmer.keepalive_seconds = cfg.gpu_guard.keepalive_seconds
        guard.on_change = warmer.notify
        guard.park = warmer.park
        guard.want_model = lambda: warmer.pinned() or m.runner.gpu_paused_waiting()
        m.runner.ram = guard.memory
        m.modules.wire_resources(guard, warmer)

    def _gpu_paused(self, reasons):
        m = self.manager
        m.modules.gpu_hold()
        for session in m.db.sessions_with_status(*ACTIVE):
            if session.get("backend", "local") == "local" and session["status"] != "waiting_approval":
                m.runner.note_gpu_pause(session["id"])

    def _gpu_resumed(self, seconds):
        m = self.manager
        m.modules.gpu_resume(m.scheduler.positions())
        m.runner.gpu_resumed(seconds)

    def start(self):
        if self.manager.guard is not None:
            self.manager.guard.start()

    async def stop(self):
        if self.manager.guard is not None:
            await self.manager.guard.stop()
        keepalive = self.service._keepalive
        self.service.unpin()
        tasks = [t for t in self.service._waking.values() if not t.done()]
        if keepalive is not None:
            tasks.append(keepalive)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def status(self):
        model = self.cfg.models.get(self.cfg.default_model)
        if model is None or self.service is None:
            return {"state": "unloaded", "loaded": False, "warming": False}
        state = await self.service.state(model)
        return {"state": state, "loaded": state == "ready", "warming": state == "waking"}

    def metrics(self, out, db):
        from .metrics import _guard_metrics
        _guard_metrics(self.manager, out)
