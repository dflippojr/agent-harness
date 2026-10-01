"""Owner-only discovery routes; never registered on the app contract."""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .discovery_paths import DiscoveryError
from .manager import HarnessError
import sys

PREFIX = '/api/admin/v1/remote-control'


class Promotion(BaseModel):
    slug: str = Field(max_length=64)
    confirmed_path: str = Field(max_length=32768)
    confirmed_markers: list[str] = Field(max_length=500)

    model_config = ConfigDict(extra='forbid')


def register(app, mgr, require_admin):
    @app.middleware('http')
    async def guard(request, call_next):
        path = request.url.path
        if request.method != 'OPTIONS' and (path.startswith(PREFIX + '/discovery/')
                                            or path.startswith(PREFIX + '/folders/')):
            try:
                require_admin(request, mgr)
            except HarnessError as error:
                return JSONResponse({'detail': str(error)}, status_code=error.status)
        return await call_next(request)

    def service(request):
        token = require_admin(request, mgr)
        manager = mgr(request)
        if sys.platform != 'win32':
            raise HarnessError(400, 'unsupported_platform')
        if manager.remote_control is None:
            raise HarnessError(409, 'remote_control_disabled')
        actor = (token or {}).get('id') or getattr(request.state.access, 'user_id', '') or 'owner'
        return manager.remote_control.discovery, str(actor)

    def invoke(call):
        try:
            return call()
        except DiscoveryError as error:
            raise HarnessError(error.status, error.code) from None

    @app.post(PREFIX + '/discovery/scans')
    async def start(request: Request):
        discovery, actor = service(request)
        try:
            return await discovery.start(actor)
        except DiscoveryError as error:
            raise HarnessError(error.status, error.code) from None

    @app.get(PREFIX + '/discovery/scans/{scan_id}')
    async def status(scan_id: str, request: Request):
        discovery, _ = service(request)
        return invoke(lambda: discovery.view(discovery.get(scan_id)))

    @app.delete(PREFIX + '/discovery/scans/{scan_id}')
    async def cancel(scan_id: str, request: Request):
        discovery, actor = service(request)
        return invoke(lambda: discovery.cancel(scan_id, actor))

    @app.post(PREFIX + '/discovery/scans/{scan_id}/candidates/{candidate_id}/promote')
    async def promote(scan_id: str, candidate_id: str, body: Promotion, request: Request):
        discovery, actor = service(request)
        return invoke(lambda: discovery.promote(scan_id, candidate_id, body.slug, body.confirmed_path,
                                               body.confirmed_markers, actor))

    @app.delete(PREFIX + '/folders/{slug}')
    async def remove(slug: str, request: Request):
        discovery, actor = service(request)
        try:
            return await discovery.remove(slug, actor)
        except DiscoveryError as error:
            raise HarnessError(error.status, error.code) from None

    return [dict(method=method, path=PREFIX + path) for method, path in [
        ('POST', '/discovery/scans'), ('GET', '/discovery/scans/{scan_id}'),
        ('DELETE', '/discovery/scans/{scan_id}'),
        ('POST', '/discovery/scans/{scan_id}/candidates/{candidate_id}/promote'),
        ('DELETE', '/folders/{slug}'),
    ]]
