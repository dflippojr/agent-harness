"""The endpoint shares the core inference gate with agent turns."""
from harness.modules import ModuleRuntime
from .settings import apply_endpoint_queue


class EndpointRuntime(ModuleRuntime):
    def init(self):
        apply_endpoint_queue(self.manager, None, None)

    def metrics(self, out, db):
        from .metrics import endpoint_metrics
        endpoint_metrics(self.manager, out, db)
