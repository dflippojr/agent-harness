"""Supervision lifecycle and wiring, reached only through the module host."""
import asyncio

from harness.modules import ModuleRuntime, ACTIVE
from .service import GpuGuard, ServerControl, hold_reminder
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
            m.guard.on_hold_reminder = self._hold_reminder
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

    def local_turn_admission(self):
        return self.admit_local_turn if self.service is not None else None

    async def admit_local_turn(self, sid, model):
        """A CLI turn on the local model (#168 Q7, Q10): wait out a GPU hold in `queued`, wait for RAM as a parked
        model's load does, then load it. Admitted, the session holds the guard's lease (runner.generating) until its
        run ends or its next turn, so a hold drains the turn before stopping the server."""
        m = self.manager
        runner = m.runner
        while True:
            await runner.wait_gpu_clear(sid)
            await runner._memory_wait(sid, model)
            runner.generating.add(sid)
            await self.service.ensure_loaded(model)
            if m.guard is not None and m.guard.active:  # a hold began while the model was loading
                runner.generating.discard(sid)
                continue
            if self.service.parked(model):
                runner.generating.discard(sid)
                if runner.model_load_low():
                    return "RAM on the server is low; the local model won't load now", "low_memory"
                if self.service.blocked():
                    return "the GPU guard has the GPU for other work; the local model can't load now", "gpu_held"
                return "the local model did not load", "model_unavailable"
            return None

    def _gpu_paused(self, reasons):
        m = self.manager
        m.modules.gpu_hold()
        for session in m.db.sessions_with_status(*ACTIVE):
            if session.get("backend", "local") == "local" and session["status"] != "waiting_approval":
                m.runner.note_gpu_pause(session["id"])

    def _hold_reminder(self, age_seconds):
        """An indefinite manual hold has lasted over a day (#732): one ntfy reminder a day while it does."""
        m = self.manager
        m.notifier.send(hold_reminder(age_seconds, m.guard.manual_since, m.cfg.notify.topic))

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
