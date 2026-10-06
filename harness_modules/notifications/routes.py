"""The notification routes, installed by the core only while the module is present (Module.owner_routes)."""

from __future__ import annotations

import httpx
from fastapi import Request

from harness.modules import HarnessError, RouteTable, manager, runtime

owner_routes = RouteTable()


@owner_routes.post("/notify/test")
async def notify_test(request: Request):
    m = manager(request)
    if not m.cfg.notify.enabled:
        raise HarnessError(400, "notifications are disabled in config/harness.yaml")
    notifier = runtime(request, "notifications").service
    payload = {"topic": m.cfg.notify.topic, "title": "Agent harness", "message": "Test notification 👋",
               "tags": ["robot"], "click": notifier.link("/")}
    async with httpx.AsyncClient(timeout=15) as client:
        await notifier.publish(client, payload)
    return {"sent": True}
