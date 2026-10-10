"""Runner transport, pairing and Mac client downloads at their original URLs."""
import logging

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from harness.modules import (HarnessError, RouteTable, manager as mgr, runtime,
                             require_admin, log_safe, compat, API_VERSION, ROOT, credential_audit,
                             refuse_hub_owner_key)

log = logging.getLogger("harness.runners")
RUNNER_BODY_LIMIT = 16 * 2**20
PAIRING_TTL_SECONDS = 10 * 60
owner_routes = RouteTable()
public_routes = RouteTable()


class RunnerPoll(BaseModel):
    instance: str
    inflight: list[str] = []
    info: dict = {}


class RunnerResult(BaseModel):
    id: str
    ok: bool
    value: object = None
    error: str = ""
    kind: str = "internal"


class RunnerPairingCodeRequest(BaseModel):
    name: str = Field(default="Agent Harness for Mac", min_length=1, max_length=60)
    runner: str = Field(default="macbook", min_length=1, max_length=60)
    ttl_seconds: int = Field(default=PAIRING_TTL_SECONDS, ge=60, le=PAIRING_TTL_SECONDS)


class RunnerPairRequest(BaseModel):
    code: str = Field(min_length=8, max_length=200)


class RunnerPairResponse(BaseModel):
    server: str
    owner_token: str
    owner_key: dict
    runner: dict
    api_version: str


# runners (the MacBook): outbound long-polling, authenticated with a per-runner bearer token
def runner_auth(request: Request, name: str):
    m = mgr(request)
    runner = m.hub.state.get(name)
    if runner is None:
        raise HarnessError(404, "unknown runner")
    if not m.hub.authorized(runner.name, request.headers.get("authorization")):
        # The configured name, not the URL's copy of it; the client address can come from proxy headers.
        log.warning("refused runner %s request from %s", runner.name,
                    log_safe(request.client.host if request.client else "?"))
        raise HarnessError(401, "bad runner token")
    return m


@owner_routes.get("/runners")
async def runners(request: Request):
    return mgr(request).hub.status()


@owner_routes.post("/runners/{name}/poll")
async def runner_poll(name: str, body: RunnerPoll, request: Request):
    m = runner_auth(request, name)
    return await m.hub.poll(name, body.instance, body.inflight, body.info)


@owner_routes.post("/runners/{name}/results")
async def runner_result(name: str, body: RunnerResult, request: Request):
    m = runner_auth(request, name)
    if int(request.headers.get("content-length") or 0) > RUNNER_BODY_LIMIT:
        raise HarnessError(413, "result too large")
    return {"accepted": m.hub.result(name, body.id, body.ok, body.value, body.error, body.kind)}


@owner_routes.post("/runners/{name}/update")
async def runner_update(name: str, request: Request):
    m = mgr(request)
    require_admin(request, mgr)
    if name not in m.hub.state:
        raise HarnessError(404, "unknown runner")
    state = m.hub.state[name]
    protocol = state.info.get("protocol")
    public = m.cfg.public_url or str(request.base_url).rstrip("/")
    fallback = (f"Run `harness update` on the Mac. If that command is unavailable, run "
                f"`curl -fsSL {public}/mac-client/install.sh | bash -s -- --server {public}`.")
    if not m.hub.online(name):
        raise HarnessError(409, f"runner is offline. {fallback}")
    try:
        remote_update_supported = 2 <= int(protocol) <= compat.PROTOCOLS["runner"]["max"]
    except (TypeError, ValueError):
        remote_update_supported = False
    if not remote_update_supported:
        raise HarnessError(409, f"runner is too old for remote update. {fallback}")
    try:
        return await m.hub.call(name, "update_client", {}, timeout=300, wait_if_offline=False)
    except Exception as exc:
        raise HarnessError(409, f"Mac client update failed: {exc}. {fallback}") from exc


@owner_routes.get("/runner-pairing-codes")
async def runner_pairing_codes(request: Request):
    """Owner view. Native pairing codes and runner tokens are never included."""
    return mgr(request).db.list_runner_pairing_codes()


@owner_routes.post("/runner-pairing-codes", status_code=201)
async def create_runner_pairing_code(body: RunnerPairingCodeRequest, request: Request):
    name = body.name.strip()
    runner = body.runner.strip()
    if not name or not runner:
        raise HarnessError(400, "name and runner are required")
    m = mgr(request)
    rt = runtime(request, "runners")
    ctx = credential_audit.request_context(request, m)
    await refuse_hub_owner_key(m, ctx, "runner_pairing.create", "pairing")  # the code redeems to an owner key
    row, code = rt.create_runner_pairing_code(name, runner, body.ttl_seconds, ctx)
    return JSONResponse({**row, "code": code}, status_code=201,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@owner_routes.delete("/runner-pairing-codes/{pid}", status_code=204)
async def revoke_runner_pairing_code(pid: str, request: Request):
    m = mgr(request)
    if not runtime(request, "runners").revoke_runner_pairing_code(
            pid, credential_audit.request_context(request, m)):
        raise HarnessError(404, "no such active runner pairing code")


@public_routes.post("/api/v1/runner-pair", status_code=201, response_model=RunnerPairResponse)
async def pair_runner(body: RunnerPairRequest, request: Request):
    """Redeem an owner-approved native Mac code without browser-origin authority."""
    paired, error = runtime(request, "runners").redeem_runner_pairing_code(body.code, str(request.base_url))
    if paired is None:
        raise HarnessError(400, error)
    return JSONResponse({**paired, "api_version": API_VERSION}, status_code=201,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_routes.get("/mac-client/install.sh", include_in_schema=False)
async def mac_client_installer():
    return FileResponse(ROOT / "macrunner" / "install.sh",
                        media_type="text/x-shellscript", headers={"Cache-Control": "no-cache"})


@public_routes.get("/mac-client/package.tar.gz", include_in_schema=False)
async def mac_client_package():
    from .mac_client import package_bytes
    return Response(package_bytes(), media_type="application/gzip", headers={
        "Content-Disposition": 'attachment; filename="agent-harness-mac.tar.gz"',
        "Cache-Control": "no-cache",
    })


@public_routes.get("/mac-client/manifest.json", include_in_schema=False)
async def mac_client_manifest():
    from .mac_client import package_manifest
    return JSONResponse(package_manifest(), headers={"Cache-Control": "no-cache"})
