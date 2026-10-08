"""Image generation's HTTP routes, installed by the core only while the module is present (Module.owner_routes and
Module.app_routes). Paths, auth and responses are the ones the daemon served before images became a module."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from harness.modules import HarnessError, RouteTable, ToolError, app_auth, manager, require_owner, runtime

from . import edit as image_edit

IMAGE_NOT_READY = "image not ready"
NO_SUCH_IMAGE = "no such image"

owner_routes = RouteTable()
app_routes = RouteTable()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "fast"
    aspect_ratio: str = "1:1"
    resolution: str = "auto"
    seed: int | None = None
    upscale: str = "none"


class ImageUpscaleRequest(BaseModel):
    upscale: str = "2x"


class ImageArchiveRetentionApply(BaseModel):
    confirmation: str


class AppImageRequest(BaseModel):
    prompt: str
    model: str = "fast"
    aspect_ratio: str = "1:1"
    upscale: str = "none"


class AppImageUpscaleRequest(BaseModel):
    upscale: str = "2x"


def images_runtime(request: Request):
    return runtime(request, "images")


# owner surface: Agent Harness Web and /api/admin/v1 (the guard middleware and require_owner do the auth)
def images_service(request: Request):
    svc = images_runtime(request).service
    if svc is None:
        raise HarnessError(400, "image generation is disabled in config/harness.yaml")
    return svc


def image_payload(job: dict, svc, request: Request, status: dict) -> dict:
    parent = svc.db.get_image(job["parent_id"]) if job.get("parent_id") else None
    children = svc.db.image_children(job["id"])
    if request.state.access.role == "guest":
        children = [child for child in children if not image_edit.is_private(child)]
    eligibility = image_edit.edit_eligibility(
        job.get("width"), job.get("height"), max_pixels=svc.cfg.max_pixels)
    edit = status.get("edit") or {}
    ready = bool(svc.edit_enabled and edit.get("available"))
    editable = ready and eligibility["editable"]
    editable_reason = (eligibility["reason"] if ready
                       else (edit.get("setup") or svc.edit_status().get("setup", "")))
    return {**job, "service": status, "private": image_edit.is_private(job),
            "editable": editable, "editable_reason": editable_reason,
            "parent": ({"id": parent["id"], "width": parent["width"], "height": parent["height"]}
                       if parent else None),
            "children": [{"id": child["id"], "operation": child.get("operation") or "generate",
                          "scale": child.get("scale"), "status": child["status"],
                          "upscale_model": child.get("upscale_model") or "", "width": child["width"],
                          "height": child["height"]} for child in children]}


def visible_job(job, request):
    if job is None:
        raise HarnessError(404, NO_SUCH_IMAGE)
    if request.state.access.role == "guest" and image_edit.is_private(job):
        raise HarnessError(404, NO_SUCH_IMAGE)
    return job


async def read_upload(file: UploadFile | None, limit: int) -> bytes:
    if file is None:
        raise HarnessError(400, "file is required")
    # Ignore the client filename entirely: uploads are stored as a generated id, never as a path.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HarnessError(400, f"image is too large (max {limit} bytes)")
    return data


@owner_routes.get("/images")
async def list_images(request: Request, limit: int = 60):
    svc = images_service(request)
    visible_operations = tuple(image_edit.PUBLIC_OPERATIONS) if request.state.access.role == "guest" else ()
    images = svc.db.list_images(limit=limit, operations=visible_operations)
    status = await asyncio.to_thread(svc.status)
    return {"status": status, "images": images}


@owner_routes.post("/images", status_code=201)
async def create_image(body: ImageRequest, request: Request):
    svc = images_service(request)
    try:
        return svc.submit(body.prompt, model=body.model, aspect_ratio=body.aspect_ratio,
                          resolution=body.resolution, seed=body.seed, upscale=body.upscale)
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/images/uploads", status_code=201)
async def upload_image(request: Request, file: UploadFile = File(...)):
    require_owner(request)
    svc = images_service(request)
    try:
        return svc.ingest_upload(await read_upload(file, svc.cfg.max_upload_bytes))
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/images/warmup")
async def warmup_images(request: Request):
    """Start ComfyUI without a checkpoint. Called when the owner opens the Images tab."""
    try:
        return await images_service(request).warmup()
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/images/cooldown")
async def cooldown_images(request: Request):
    """Drop an unused Images-tab warmup so the language model can come back."""
    return images_service(request).cooldown()


@owner_routes.post("/images/{iid}/edit", status_code=201)
async def edit_image(iid: str, request: Request, prompt: str = Form(...), mask: UploadFile = File(...),
                     feather: int = Form(0), seed: int | None = Form(None)):
    require_owner(request)
    svc = images_service(request)
    parent = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return svc.submit_edit(parent["id"], prompt, await read_upload(mask, svc.cfg.max_upload_bytes),
                               feather=feather, seed=seed)
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/images/{iid}/upscale", status_code=201)
async def upscale_image(iid: str, body: ImageUpscaleRequest, request: Request):
    svc = images_service(request)
    parent = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return svc.submit_upscale(parent["id"], body.upscale)
    except ToolError as e:
        raise HarnessError(400, str(e))


@owner_routes.post("/images/{iid}/cancel")
async def cancel_image(iid: str, request: Request):
    require_owner(request)
    svc = images_service(request)
    job = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    try:
        return await svc.cancel(job["id"])
    except ToolError as e:
        raise HarnessError(409, str(e))


@owner_routes.delete("/images/{iid}")
async def delete_image(iid: str, request: Request):
    require_owner(request)
    svc = images_service(request)
    job = visible_job(svc.db.get_image(iid.removesuffix(".png")), request)
    backup = Path(manager(request).cfg.backup.dir) if manager(request).cfg.backup.dir else None
    try:
        return await svc.delete(job["id"], backup_dir=backup)
    except ToolError as e:
        raise HarnessError(404, str(e))


@owner_routes.get("/images/{iid}")
async def get_image(iid: str, request: Request):
    svc = images_service(request)
    variant = "json"
    raw = iid
    if raw.endswith(".source.png"):
        variant, raw = "source", raw[: -len(".source.png")]
    elif raw.endswith(".mask.png"):
        variant, raw = "mask", raw[: -len(".mask.png")]
    elif raw.endswith(".png"):
        variant, raw = "png", raw[: -len(".png")]
    job = visible_job(svc.db.get_image(raw), request)
    owner = request.state.access.role == "owner"
    if variant == "json":
        status = await asyncio.to_thread(svc.status)
        return image_payload(job, svc, request, status)
    if variant in ("source", "mask") and not owner:
        raise HarnessError(404, IMAGE_NOT_READY)
    path = {"png": svc.path, "source": svc.source_path, "mask": svc.mask_path}[variant](job)
    if variant == "png" and job["status"] != "done":
        raise HarnessError(404, IMAGE_NOT_READY)
    if not path.exists():
        raise HarnessError(404, IMAGE_NOT_READY)
    headers = {"Cache-Control": "private, no-store"} if image_edit.is_private(job) or variant != "png" else {
        "Cache-Control": "max-age=86400"}
    return FileResponse(path, media_type="image/png", headers=headers)


@owner_routes.post("/maintenance/image-archive/retention/preview")
async def image_archive_retention_preview(request: Request):
    require_owner(request)
    return await asyncio.to_thread(images_runtime(request).archive.retention_preview)


@owner_routes.post("/maintenance/image-archive/retention/apply")
async def image_archive_retention_apply(body: ImageArchiveRetentionApply, request: Request):
    from .archive import ImageArchiveError
    require_owner(request)
    try:
        return await asyncio.to_thread(images_runtime(request).archive.apply_retention, body.confirmation)
    except ImageArchiveError as e:
        raise HarnessError(409, str(e))


# App API (/api/v1): app_auth checks the token and its images scope
@app_routes.post("/api/v1/images", status_code=201)
async def app_image(body: AppImageRequest, request: Request):
    key = app_auth(request, "images")
    if key.get("kind") == "member":
        raise HarnessError(403, "members cannot use image generation")
    svc = images_runtime(request).service
    if svc is None:
        raise HarnessError(400, "image generation is disabled on this harness")
    try:
        job = svc.submit(body.prompt, model=body.model, aspect_ratio=body.aspect_ratio,
                         source=f"app:{key['name']}"[:40], upscale=body.upscale)
    except ToolError as e:
        raise HarnessError(400, str(e))
    return {**job, "url": f"/api/v1/images/{job['id']}.png"}


@app_routes.post("/api/v1/images/{iid}/upscale", status_code=201)
async def app_image_upscale(iid: str, body: AppImageUpscaleRequest, request: Request):
    key = app_auth(request, "images")
    svc = images_runtime(request).service
    if svc is None:
        raise HarnessError(400, "image generation is disabled on this harness")
    parent = svc.db.get_image(iid.removesuffix(".png"))
    if parent is None or image_edit.is_private(parent):
        raise HarnessError(404, NO_SUCH_IMAGE)
    try:
        job = svc.submit_upscale(parent["id"], body.upscale, source=f"app:{key['name']}"[:40])
    except ToolError as e:
        raise HarnessError(400, str(e))
    return {**job, "url": f"/api/v1/images/{job['id']}.png"}


@app_routes.get("/api/v1/images/{iid}")
async def app_image_status(iid: str, request: Request):
    app_auth(request, "images")
    svc = images_runtime(request).service
    job = svc.db.get_image(iid.removesuffix(".png")) if svc else None
    if job is None:
        raise HarnessError(404, NO_SUCH_IMAGE)
    if image_edit.is_private(job):
        raise HarnessError(404, NO_SUCH_IMAGE)
    if iid.endswith(".png"):
        if job["status"] != "done":
            raise HarnessError(404, IMAGE_NOT_READY)
        return FileResponse(svc.path(job), media_type="image/png")
    children = [child for child in svc.db.image_children(job["id"]) if not image_edit.is_private(child)]
    return {**job, "url": f"/api/v1/images/{job['id']}.png" if job["status"] == "done" else None,
            "children": [{"id": c["id"], "scale": c.get("scale"), "status": c["status"],
                          "upscale_model": c.get("upscale_model") or ""} for c in children]}
