"""The backup route, installed by the core only while the module is present (Module.owner_routes)."""

from __future__ import annotations

from fastapi import Request

from harness.modules import RouteTable, require_owner, runtime, operation_audit

owner_routes = RouteTable()


@owner_routes.post("/maintenance/backup")
async def maintenance_backup(request: Request):
    m = require_owner(request)
    return await runtime(request, "backup").service.backup(context=operation_audit.request_context(request, m))
