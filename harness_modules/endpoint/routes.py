"""Unchanged inference URLs, authenticated with inference keys."""
from fastapi import Request
from harness.modules import RouteTable, manager
from .service import ROUTES, error, flavor_of, authenticate, _models_payload, _capabilities_payload, proxy, BAD_KEY

public_routes = RouteTable()

@public_routes.get("/v1/models")
async def v1_models(request: Request):
    m = manager(request)
    if not m.cfg.endpoint.enabled:
        return error(flavor_of(request), 404, "not_found_error", "the inference endpoint is disabled")
    if authenticate(m, request) is None:
        return error(flavor_of(request), 401, "authentication_error", BAD_KEY)
    return _models_payload(m)

@public_routes.get("/v1/capabilities")
async def v1_capabilities(request: Request):
    m = manager(request)
    if not m.cfg.endpoint.enabled or authenticate(m, request) is None:
        return error("openai", 401, "authentication_error", BAD_KEY)
    return _capabilities_payload(m)

def route_handler(path: str):
    async def handler(request: Request):
        return await proxy(manager(request), request, path)
    return handler

for path in ROUTES:
    public_routes.post(path, include_in_schema=False)(route_handler(path))
