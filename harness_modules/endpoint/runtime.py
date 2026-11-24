"""The endpoint shares the core inference gate with agent turns."""
from harness.modules import ModuleRuntime
from .settings import apply_endpoint_queue
from .store import EndpointRequestStore


class EndpointRuntime(ModuleRuntime):
    def init(self):
        apply_endpoint_queue(self.manager, None, None)

    def key_request_counts(self, key_ids: list[str]) -> dict[str, int]:
        """The request log is read whether or not `endpoint.enabled` is on: past requests still count."""
        return EndpointRequestStore(self.manager.db).request_counts(key_ids)

    def metrics(self, out, db):
        from .metrics import endpoint_metrics
        endpoint_metrics(self.manager, out, db)
