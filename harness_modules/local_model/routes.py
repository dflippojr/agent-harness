"""Local model warm-up and machine resource management routes."""
from fastapi import Request
from pydantic import BaseModel
from harness.modules import RouteTable, manager, HarnessError, app_auth, LOW_MEMORY, PAUSED

MODELS_WARM = "models:warm"
owner_routes, app_routes = RouteTable(), RouteTable()

class GpuHoldRequest(BaseModel):
    duration_seconds: int | None = None
    force: bool = False   # load: go ahead although available RAM is under the guard's threshold


@owner_routes.get("/models/status")
async def models_status(request: Request):
    m = manager(request)
    return [{"name": mc.name, "state": await m.warmer.state(mc), "waking_seconds": m.warmer.waking_for(mc)}
            for mc in m.cfg.models.values()]


@owner_routes.post("/models/warm")
async def models_warm(request: Request):
    """Load the default model if it's asleep. The web app calls this when it opens."""
    m = manager(request)
    if not m.cfg.modules.local_model:
        raise HarnessError(400, "the local model is disabled by this service profile")
    model = m.cfg.models[m.cfg.default_model]
    return {"name": model.name, "state": await m.warmer.warm(model)}


# Resource guard (formerly the GPU guard; /gpu stays as an alias). docs/resource-guard.md
async def _resources_status(m) -> dict:
    from .resources import model_status
    status = m.guard.status() if m.guard else {"enabled": False, "state": "clear", "signals": []}
    return {**status, "model": await model_status(m),
            "load_now_default_minutes": m.cfg.gpu_guard.load_now_default_minutes}


@owner_routes.get("/gpu")
@owner_routes.get("/resources")
async def gpu(request: Request):
    return await _resources_status(manager(request))


@owner_routes.get("/resources/diagnostics")
async def resources_diagnostics(request: Request):
    """One reading for Actions -> Resources (VRAM, RAM, GPU/CPU load, model and guard state). Not polled."""
    from .resources import diagnostics
    return await diagnostics(manager(request))


async def _load_now(m, body: GpuHoldRequest | None) -> None:
    if not m.cfg.modules.local_model:
        raise HarnessError(400, "the local model is disabled by this service profile")
    if m.guard.active or m.guard.manual:
        raise HarnessError(409, "the GPU is held; turn the hold off first")
    minutes = m.cfg.gpu_guard.load_now_default_minutes
    duration = body.duration_seconds if body and body.duration_seconds else minutes * 60
    if not 60 <= duration <= 24 * 60 * 60:
        raise HarnessError(400, "duration_seconds must be between 60 and 86400")
    if m.runner.model_load_low() and not (body and body.force):
        from .service import describe_memory
        raise HarnessError(409, f"low memory: {describe_memory(m.guard.memory.status())}; loading the model "
                                "takes about 14 GB more. Send force to load anyway", code="low_memory")
    await m.warmer.load_now(m.cfg.models[m.cfg.default_model], duration)


@owner_routes.post("/gpu/{action}")
@owner_routes.post("/resources/{action}")
async def gpu_action(action: str, request: Request, body: GpuHoldRequest | None = None):
    """pause: hold the GPU for other uses until resumed. resume: end the hold, ignoring the current triggers (the
    model stays unloaded until something needs it). load: load the model now and keep it loaded for
    duration_seconds. unload: unload it now without holding the queue."""
    m = manager(request)
    if m.guard is None:
        raise HarnessError(400, "the resource guard is disabled in config/harness.yaml")
    if action == "load":
        await _load_now(m, body)
    elif action == "unload":
        # Unpinned only if the unload goes ahead: a refused one keeps "Load local model now" and its keepalive.
        if not await m.guard.unload(before_stop=m.warmer.unpin):
            raise HarnessError(409, "a model turn is running or the GPU is held; try again when it's idle")
    elif action == "pause":
        duration = body.duration_seconds if body else None
        if duration is not None and not 1 <= duration <= 24 * 60 * 60:
            raise HarnessError(400, "duration_seconds must be between 1 and 86400")
        m.guard.pause(duration)
    elif action == "resume":
        # Turning off the manual hold must not suppress a live game/Plex trigger. A direct resume while only an
        # automatic trigger is active retains the legacy "resume anyway" operator action.
        m.guard.resume(override_signals=not m.guard.manual)
    else:
        raise HarnessError(404, "unknown action")
    return await _resources_status(m)


@app_routes.get("/api/v1/models/status")
async def api_models_status(request: Request):
    m = manager(request)
    app_auth(request, "sessions")
    return [{"name": mc.name, "state": await m.warmer.state(mc), "waking_seconds": m.warmer.waking_for(mc)}
            for mc in m.cfg.models.values()]


@app_routes.post("/api/v1/models/warm")
async def api_models_warm(request: Request):
    m = manager(request)
    key = app_auth(request, "sessions")
    app = key.get("kind") == "app"
    if app and MODELS_WARM not in key["scope_set"]:
        raise HarnessError(403, f"this token lacks the {MODELS_WARM!r} scope")
    if not m.cfg.modules.local_model:
        raise HarnessError(400, "the local model is disabled by this service profile")
    model = m.cfg.models[m.cfg.default_model]
    state = await m.warmer.warm(model)
    # An App is refused, never queued, while a guard holds the model back (#329); the owner's app shows the state.
    if app and state == PAUSED:
        raise HarnessError(409, "the GPU guard has the GPU for other work; the local model can't load now", "gpu_held")
    if app and state == LOW_MEMORY:
        raise HarnessError(409, "RAM on the server is low; the local model won't load now", "low_memory")
    return {"name": model.name, "state": state}


