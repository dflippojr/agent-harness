"""Optional OpenAI/Anthropic-compatible local inference and embeddings proxy."""
from harness.modules import Module, enabled_flag, lazy


MODULE = Module(
    name="endpoint", switches=("endpoint",), title="Inference endpoint",
    docs=("docs/modules.md",), runtime=lazy(".runtime", "EndpointRuntime", call=True),
    runtime_enabled=enabled_flag("endpoint"),
    public_routes=lazy(".routes", "public_routes"), settings=lazy(".settings", "specs", call=True),
)
