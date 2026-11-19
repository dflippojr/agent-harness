"""The backup route, installed by the core only while the module is present (Module.owner_routes)."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from fastapi import Request
from pydantic import ConfigDict, with_config

from harness.modules import RouteTable, documented, require_owner, runtime, operation_audit

owner_routes = RouteTable()


@with_config(ConfigDict(extra="allow"))  # documentation only: a reply may carry more fields
class BackupResponse(TypedDict):
    """The backup just written; `error` is empty when it succeeded."""
    ok_at: float | None
    path: str
    bytes: int
    error: str
    removed: NotRequired[list[str]]
    app_stores: NotRequired[int]
    transcript_archives: NotRequired[int]
    warnings: NotRequired[list[str]]
    files: NotRequired[int]


@owner_routes.post("/maintenance/backup", responses=documented(BackupResponse))
async def maintenance_backup(request: Request):
    """Write a dated backup of the database, transcripts, and config, wait for it, then prune old backups."""
    m = require_owner(request)
    return await runtime(request, "backup").service.backup(context=operation_audit.request_context(request, m))
