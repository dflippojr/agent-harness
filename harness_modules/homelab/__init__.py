"""Allowlisted homelab tools, installed through the module interface."""
from harness.modules import Module, lazy


MODULE = Module(
    name="homelab",
    switches=("homelab",),
    title="Homelab",
    docs=("docs/modules.md",),
    runtime=lazy(".runtime", "HomelabRuntime", call=True),
    app_scopes={"homelab": "use the host homelab tools (logs, service config, metrics) in homelab projects"},
    app_capabilities={"homelab": "homelab"},
    tool_names=("homelab_services", "container_logs", "read_service_config", "prometheus_query",
                "restart_service", "rebuild_service"),
)
