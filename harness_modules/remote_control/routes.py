"""Existing owner and App Remote Control contracts."""
from fastapi import Request
from harness.modules import RouteTable, manager, HarnessError, ToolError, app_auth

owner_routes = RouteTable()
app_routes = RouteTable()

# Claude Code Remote Control servers
def remote_control(m):
    if m.remote_control is None:
        raise HarnessError(400, "Remote Control launches are disabled (remote_control.enabled in harness.yaml)")
    return m.remote_control


def rc_owner_surface(request):
    return request.scope.get('harness_original_path', '').startswith('/api/admin/v1/')


@owner_routes.get("/remote-control")
async def rc_status(request: Request):
    m = manager(request)
    if m.remote_control is None:
        return {"enabled": False, "projects": []}
    if request.state.access.role == "guest":
        return {"enabled": True, "projects": []}
    owner = rc_owner_surface(request)
    result = {"enabled": True, "projects": m.remote_control.status(include_owner_only=owner)}
    if owner:
        import sys
        from .folder_discovery import LIMITS
        result['discovery'] = dict(supported=sys.platform == 'win32',
                                   enabled=m.cfg.remote_control.discovery.enabled, limits=dict(LIMITS))
    return result


@owner_routes.post("/remote-control/{project}")
async def rc_launch(project: str, request: Request):
    from harness.modules import ToolError
    rc = remote_control(manager(request))
    try:
        return await rc.launch(project, started_by=getattr(request.state.access, 'user_id', '') or 'owner',
                               include_owner_only=rc_owner_surface(request))
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/remote-control/{project}/trust")
async def rc_trust(project: str, request: Request):
    from harness.modules import ToolError
    try:
        return remote_control(manager(request)).open_trust_prompt(project, include_owner_only=rc_owner_surface(request),
                                                            actor=getattr(request.state.access, 'user_id', '') or 'owner')
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/remote-control/{project}/stop")
async def rc_stop(project: str, request: Request):
    from harness.modules import ToolError
    try:
        return await remote_control(manager(request)).stop(project, include_owner_only=rc_owner_surface(request))
    except ToolError as e:
        raise HarnessError(404, str(e))
@app_routes.get("/api/v1/remote-control")
async def app_rc_status(request: Request):
    m = manager(request)
    key = app_auth(request, "remote_control")
    if key.get("kind") == "member":
        raise HarnessError(403, "members cannot use Remote Control")
    return {"enabled": m.remote_control is not None,
            "projects": m.remote_control.status() if m.remote_control else []}


@app_routes.post("/api/v1/remote-control/{project}")
async def app_rc_launch(project: str, request: Request):
    m = manager(request)
    key = app_auth(request, "remote_control")
    if m.remote_control is None:
        raise HarnessError(400, "Remote Control launches are disabled on this harness")
    try:
        return await m.remote_control.launch(project, started_by=f"app:{key['name']}")
    except ToolError as e:
        raise HarnessError(400, str(e))


@app_routes.post("/api/v1/remote-control/{project}/stop")
async def app_rc_stop(project: str, request: Request):
    m = manager(request)
    app_auth(request, "remote_control")
    if m.remote_control is None:
        raise HarnessError(400, "Remote Control launches are disabled on this harness")
    try:
        return await m.remote_control.stop(project)
    except ToolError as e:
        raise HarnessError(404, str(e))

