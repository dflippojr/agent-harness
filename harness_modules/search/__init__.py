"""Full-text search over past sessions, as an add-on module (#334; docs/modules.md).

Contributes GET /search, GET /api/v1/search, the session_search / session_read agent tools, the ``search.enabled``
setting and the ``harness search`` CLI row. The FTS index itself stays in the session database (harness/db.py,
harness/search_index.py), so this file stays light: the CLI imports it to list commands.
"""

from __future__ import annotations

from harness.modules import Module, ToolGate

SEARCH_PROMPT = ("Past work: session_search finds earlier agent sessions on this server and session_read reads one. "
                 "Use them when the task mentions earlier work or a past fix would help; they may be outdated.")


def _runtime(manager, module):
    from .runtime import SearchRuntime
    return SearchRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _app_routes():
    from .routes import app_routes
    return app_routes


def _settings():
    from .settings import specs
    return specs()


def _runtime_enabled(cfg, switch: str) -> bool:
    return bool(cfg.search.enabled)


MODULE = Module(
    name="search",
    switches=("search",),
    title="Session search",
    docs=("docs/modules.md",),
    runtime_enabled=_runtime_enabled,
    runtime=_runtime,
    owner_routes=_owner_routes,
    admin_paths=frozenset({"/search"}),
    app_routes=_app_routes,
    tools=ToolGate(project_flag="session_search", capability="search", members=True, mcp=True,
                   prompt=SEARCH_PROMPT),
    tool_names=("session_search", "session_read"),
    settings=_settings,
    cli=(("search", "GET", "/search", "search sessions", ("q", "--project", "--limit:int")),),
)
