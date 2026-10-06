"""Allowlisted homelab tools, installed through the module interface."""
from harness.modules import Module


def _runtime(manager, module):
    from .runtime import HomelabRuntime
    return HomelabRuntime(manager, module)


MODULE = Module(
    name="homelab",
    switches=("homelab",),
    title="Homelab",
    docs=("docs/modules.md",),
    runtime=_runtime,
    tool_names=("homelab_services", "container_logs", "read_service_config", "prometheus_query",
                "restart_service", "rebuild_service"),
)
