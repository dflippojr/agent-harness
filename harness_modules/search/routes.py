"""Session search routes, installed by the core only while the module is present."""

from __future__ import annotations

import asyncio

from fastapi import Request

from harness.modules import HarnessError, RouteTable, app_auth, calling_app, manager, owner_id, reaches_web

from . import service as search

owner_routes = RouteTable()
app_routes = RouteTable()


@owner_routes.get("/search")
async def search_sessions(request: Request, q: str = "", project: str = "", limit: int = 20):
    """Full-text search over past sessions. Passages mark matches with \\u0002 ... \\u0003."""
    m = manager(request)
    if request.state.access.role == "guest":
        return {"query": q, "mode": "all", "results": []}
    if not m.cfg.search.enabled:
        raise HarnessError(400, "session search is disabled in config/harness.yaml")
    return await asyncio.to_thread(search.search, m.db, q, project, max(1, min(limit, 50)),
                                   "", owner_id(request))


@app_routes.get("/api/v1/search")
async def api_search(request: Request, q: str = "", project: str = "", limit: int = 20):
    m = manager(request)
    key = app_auth(request, "sessions")
    user_id = key["user_id"] if key.get("kind") == "member" else "owner"
    app_id = None
    if not reaches_web(key):
        app_id = key["id"]
    if not m.cfg.search.enabled:
        raise HarnessError(400, "session search is disabled in config/harness.yaml")
    return await asyncio.to_thread(
        search.search, m.db, q, project, max(1, min(limit, 50)), "", user_id, app_id, with_app=calling_app(key))
