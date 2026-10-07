"""Owner-pinned stdio MCP tools for the native loop (#260)."""

from harness.modules import Module, ToolGate


def _runtime(manager, module):
    from .runtime import McpRuntime
    return McpRuntime(manager, module)


MODULE = Module(
    name="mcp_client", switches=("mcp_client",), title="Owner-pinned MCP client",
    runtime=_runtime, docs=("docs/mcp-client.md",),
    tools=ToolGate(project_flag="mcp_servers", capability="", mcp=False,
                   per_session=True, eligible=lambda kit, session: True, span="mcp_tool_call"),
)
