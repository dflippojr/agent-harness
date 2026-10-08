"""Optional OpenAI/Anthropic-compatible local inference and embeddings proxy."""
from harness.modules import Module


def _runtime(manager, module):
    from .runtime import EndpointRuntime
    return EndpointRuntime(manager, module)


def _routes():
    from .routes import public_routes
    return public_routes


def _settings():
    from .settings import specs
    return specs()


MODULE = Module(
    name="endpoint", switches=("endpoint",), title="Inference endpoint",
    docs=("docs/modules.md",), runtime=_runtime,
    runtime_enabled=lambda cfg, switch: bool(cfg.endpoint.enabled),
    public_routes=_routes, settings=_settings,
)
