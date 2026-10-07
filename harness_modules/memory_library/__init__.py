"""Personal memory library, installed through the optional module interface."""
from harness.modules import Module, ToolGate


def _runtime(manager, module):
    from .runtime import MemoryLibraryRuntime
    return MemoryLibraryRuntime(manager, module)


def _routes():
    from .routes import owner_routes
    return owner_routes


def _settings():
    from .settings import specs
    return specs()


def _enabled(cfg, switch):
    return bool(cfg.memory_library.enabled)


MODULE = Module(
    name="memory_library",
    switches=("memory_library",),
    title="Memory library",
    docs=("docs/modules.md",),
    runtime=_runtime,
    runtime_enabled=_enabled,
    owner_routes=_routes,
    admin_paths=frozenset({"/memory", "/memory/profile"}),
    tools=ToolGate(project_flag="memory_library", capability="memory_library"),
    tool_names=("memory_index", "memory_search", "memory_read", "memory_edit", "memory_write"),
    settings=_settings,
    cli=(("memory show", "GET", "/memory", "show the memory library", ()),
         ("memory set-profile", "PUT", "/memory/profile", "replace the agent profile", ("content", "--summary"))),
    cli_groups={"memory": "memory library"},
)
