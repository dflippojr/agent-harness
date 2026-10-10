"""Owner-only read access for the CLI and standalone Hub key."""
from fastapi import Request
from pydantic import BaseModel

from harness.modules import HarnessError, runtime
from .entries import EntriesError
from .service import inventory


class HubInventory(BaseModel):
    modules: list[dict]
    apps: list[dict]
    entries: list[dict]


def register_admin(app, mgr, require_admin):
    @app.get("/api/admin/v1/hub", response_model=HubInventory)
    async def hub_inventory(request: Request):
        """Read module, paired-app and local unsigned-entry status and metadata."""
        require_admin(request, mgr)
        rt = runtime(request, "hub")
        if not rt.effective():
            raise HarnessError(400, "Hub inventory is disabled")
        try:
            return await inventory(mgr(request))
        except EntriesError as exc:
            raise HarnessError(400, str(exc)) from None

    return [{"method": "GET", "path": "/api/admin/v1/hub"}]
