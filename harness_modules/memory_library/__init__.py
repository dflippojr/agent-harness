"""Personal memory library, installed through the optional module interface."""
from harness.modules import Module, ToolGate, enabled_flag, lazy


MODULE = Module(
    name="memory_library",
    switches=("memory_library",),
    title="Memory library",
    docs=("docs/modules.md",),
    runtime=lazy(".runtime", "MemoryLibraryRuntime", call=True),
    runtime_enabled=enabled_flag("memory_library"),
    owner_routes=lazy(".routes", "owner_routes"),
    admin_paths=frozenset({"/memory", "/memory/profile"}),
    app_scopes={"memory_library": "read the owner's memory library and propose edits to it in the app's sessions"},
    app_capabilities={"memory_library": "memory_library"},
    tools=ToolGate(project_flag="memory_library", capability="memory_library"),
    tool_names=("memory_index", "memory_search", "memory_read", "memory_edit", "memory_write"),
    settings=lazy(".settings", "specs", call=True),
    cli=(("memory show", "GET", "/memory", "show the memory library", ()),
         ("memory set-profile", "PUT", "/memory/profile", "replace the agent profile", ("content", "--summary"))),
    cli_groups={"memory": "memory library"},
)
