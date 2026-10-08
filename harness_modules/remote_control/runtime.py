"""Remote Control lifecycle; servers deliberately survive daemon shutdown."""
from harness.modules import ModuleRuntime
from .service import RemoteControl


class RemoteControlRuntime(ModuleRuntime):
    def init(self):
        if self.effective():
            self.service = RemoteControl(self.cfg, self.cfg.remote_control, notify=self._ready)
            self.service.discovery.settings = self.manager.settings

    def _ready(self, payload):
        self.manager.notifier.send({'topic': self.cfg.notify.topic, **payload})

    def toolkit(self):
        return self.service

    def features(self):
        return {'remote_control': self.service is not None}
