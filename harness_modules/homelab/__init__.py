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
    app_scopes={"homelab": "use the host homelab tools (logs, service config, metrics) in homelab projects"},
    app_capabilities={"homelab": "homelab"},
    tool_names=("homelab_services", "container_logs", "read_service_config", "prometheus_query",
                "restart_service", "rebuild_service"),
)
