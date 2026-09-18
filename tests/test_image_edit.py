"""Issue #88: optional masked inpainting / photo editing with Qwen-Image-Edit."""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from harness import config, image_edit, setup_config
from harness.api import create_app
from harness.config import GuestAccess
from harness.fileops import ToolError

from test_phase6 import image_manager

LOGIN = "me@example.com"


def png_rgb(width=64, height=64, color=(12, 24, 48), exif=None) -> bytes:
    image = Image.new("RGB", (width, height), color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def png_mask(width=64, height=64, empty=False) -> bytes:
    image = Image.new("L", (width, height), 0)
    if not empty:
        for x in range(8, 24):
            for y in range(8, 24):
                image.putpixel((x, y), 255)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def jpeg_with_exif() -> bytes:
    image = Image.new("RGB", (160, 96), (200, 10, 10))
    out = io.BytesIO()
    # Orientation 6: 90° CW. exif_transpose should swap to 96×160 before we snap to 16px.
    exif = image.getexif()
    exif[274] = 6
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def stub_edit_assets(tmp_path: Path, cfg) -> Path:
    root = tmp_path / "comfy-models"
    (root / "diffusion_models").mkdir(parents=True)
    (root / "text_encoders").mkdir()
    (root / "vae").mkdir()
    (root / "diffusion_models" / image_edit.EDIT_MODEL["unet"]["name"]).write_bytes(b"stub-unet")
    (root / "text_encoders" / image_edit.EDIT_MODEL["clip"]["name"]).write_bytes(b"stub-clip")
    (root / "vae" / image_edit.EDIT_MODEL["vae"]["name"]).write_bytes(b"stub-vae")
    cfg.models_dir = str(root)
    cfg.edit_enabled = True
    return root


def edit_manager(tmp_path):
    m, server, state = image_manager(tmp_path)
    stub_edit_assets(tmp_path, m.images.cfg)
    return m, server, state


def test_normalize_rejects_unsupported_and_empty_mask():
    with pytest.raises(ToolError, match="malformed"):
        image_edit.normalize_source(b"not-an-image")
    gif = Image.new("RGB", (16, 16), (1, 2, 3))
    buf = io.BytesIO()
    gif.save(buf, format="GIF")
    with pytest.raises(ToolError, match="unsupported"):
        image_edit.normalize_source(buf.getvalue())
    png, width, height = image_edit.normalize_source(png_rgb())
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and width == 64 and height == 64
    with pytest.raises(ToolError, match="empty"):
        image_edit.normalize_mask(png_mask(empty=True), 64, 64)
    with pytest.raises(ToolError, match="does not match"):
        image_edit.normalize_mask(png_mask(32, 32), 64, 64)
    with pytest.raises(ToolError, match="pixel cap"):
        image_edit.normalize_source(png_rgb(64, 64), max_pixels=10)


def test_normalize_strips_exif_and_ignores_client_path_bytes():
    data, width, height = image_edit.normalize_source(jpeg_with_exif())
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG" and 274 not in image.getexif()
    assert image.mode == "RGB"
    # 80×48 with orientation 6 becomes 48×80, then floored to a multiple of 16.
    assert width % 16 == 0 and height % 16 == 0
    assert b"Exif" not in data


def test_full_profile_does_not_enable_image_edit_by_default(tmp_path):
    args = ["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"), "--model", "gpt-oss",
            "--pause-flag", str(tmp_path / "paused")]
    assert setup_config.main(args) == 0
    cfg = config.load(tmp_path / "cfg")
    assert not cfg.modules.image_edit and not cfg.images.edit_enabled
    setup_config.main(args + ["--enable-module", "image_edit"])
    enabled = config.load(tmp_path / "cfg")
    assert enabled.modules.image_edit and enabled.images.edit_enabled and enabled.modules.images


def test_edit_status_is_setup_guidance_without_assets(tmp_path):
    m, _, _ = image_manager(tmp_path)
    status = m.images.status()["edit"]
    assert status["available"] is False and status["enabled"] is False
    assert "Qwen-Image-Edit" in status["setup"]
    assert "Apache 2.0" in status["setup"]
    assert status["revision"] == image_edit.EDIT_MODEL["revision"]
    assert status["sha256"] == image_edit.EDIT_MODEL["unet"]["sha256"]
    assert not any(sep in status["setup"] for sep in ("C:\\", "/home/", "tailnet"))


def test_gallery_and_upload_edits_preserve_source(tmp_path):
    async def body():
        m, _, state = edit_manager(tmp_path)
        await m.start(maintenance=False)
        parent = m.images.submit("a red cube", model="fast")
        parent = await m.images.wait(parent["id"])
        original = m.images.path(parent).read_bytes()
        uploaded = m.images.ingest_upload(png_rgb(96, 64, (1, 2, 3)))
        assert uploaded["operation"] == "upload" and uploaded["status"] == "done"
        assert uploaded["width"] == 96 and uploaded["height"] == 64
        gallery_edit = m.images.submit_edit(parent["id"], "replace the cube with a sphere", png_mask(parent["width"], parent["height"]))
        upload_edit = m.images.submit_edit(uploaded["id"], "add a blue sky", png_mask(uploaded["width"], uploaded["height"]))
        done = [await m.images.wait(j["id"]) for j in (gallery_edit, upload_edit)]
        assert [j["status"] for j in done] == ["done", "done"]
        assert done[0]["parent_id"] == parent["id"] and done[1]["parent_id"] == uploaded["id"]
        assert done[0]["model_revision"] == image_edit.EDIT_MODEL["revision"]
        assert m.images.path(parent).read_bytes() == original
        assert m.images.source_path(done[0]).read_bytes() == original
        assert m.images.mask_path(done[0]).exists()
        graphs = state["graphs"][-2:]
        assert all(n["inputs"]["unet_name"] == "qwen_image_edit_fp8_e4m3fn.safetensors"
                   for g in graphs for n in g.values() if n["class_type"] == "UNETLoader")
        assert all(n["inputs"]["prompt"] != "" for g in graphs for n in g.values()
                   if n["class_type"] == "TextEncodeQwenImageEdit" and n["inputs"].get("prompt", " ").strip())
        await m.stop()
    asyncio.run(body())


def test_edit_validation_and_path_tricks_before_gpu(tmp_path):
    m, server, _ = edit_manager(tmp_path)
    parent = m.images.ingest_upload(png_rgb())
    with pytest.raises(ToolError, match="prompt is empty"):
        m.images.submit_edit(parent["id"], "  ", png_mask())
    with pytest.raises(ToolError, match="empty"):
        m.images.submit_edit(parent["id"], "fix the sky", png_mask(empty=True))
    with pytest.raises(ToolError, match="does not match"):
        m.images.submit_edit(parent["id"], "fix the sky", png_mask(32, 32))
    with pytest.raises(ToolError, match="malformed"):
        m.images.ingest_upload(b"MZ-not-an-image")
    # A client-supplied path is ignored; only the bytes matter.
    job = m.images.ingest_upload(png_rgb(color=(9, 9, 9)))
    assert ".." not in job["id"] and m.images.path(job).parent == m.images.images_dir
    assert server.calls == []


def test_edit_missing_assets_do_not_break_text_to_image(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path)
        await m.start(maintenance=False)
        with pytest.raises(ToolError, match="image-edit component"):
            m.images.ingest_upload(png_rgb())
        job = await m.images.wait(m.images.submit("still works")["id"])
        assert job["status"] == "done" and job["operation"] == "generate"
        await m.stop()
    asyncio.run(body())


def test_owner_guest_app_authorization(tmp_path):
    m, _, _ = edit_manager(tmp_path)
    m.cfg.allowed_logins = [LOGIN]
    m.cfg.guests = [GuestAccess(login="buddy@example.com")]
    with TestClient(create_app(m)) as client:
        uploaded = client.post("/images/uploads", files={"file": ("../../etc/passwd.png", png_rgb(), "image/png")})
        assert uploaded.status_code == 201
        uid = uploaded.json()["id"]
        assert client.get("/images").json()["images"][0]["id"] == uid
        edit = client.post(f"/images/{uid}/edit", data={"prompt": "make it dusk", "feather": "0"},
                           files={"mask": ("mask.png", png_mask(), "image/png")})
        assert edit.status_code == 201
        eid = edit.json()["id"]
        for _ in range(200):
            if client.get(f"/images/{eid}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert client.get(f"/images/{eid}").json()["status"] == "done"
        assert client.get(f"/images/{eid}.source.png").status_code == 200
        assert client.get(f"/images/{eid}.mask.png").status_code == 200
        assert "no-store" in client.get(f"/images/{eid}.png").headers.get("cache-control", "")

        gh = {"Tailscale-User-Login": "buddy@example.com"}
        listing = client.get("/images", headers=gh).json()["images"]
        assert all(img["id"] not in {uid, eid} for img in listing)
        assert client.get(f"/images/{uid}", headers=gh).status_code == 404
        assert client.get(f"/images/{eid}.png", headers=gh).status_code == 404
        assert client.get(f"/images/{eid}.mask.png", headers=gh).status_code == 404
        assert client.post("/images/uploads", headers=gh, files={"file": ("x.png", png_rgb(), "image/png")}).status_code == 403

        gen = client.post("/images", json={"prompt": "public cube"}).json()
        for _ in range(200):
            if client.get(f"/images/{gen['id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        assert client.get(f"/images/{gen['id']}.png", headers=gh).status_code == 200
        guest_list = client.get("/images", headers=gh).json()["images"]
        assert gen["id"] in {img["id"] for img in guest_list}

        private_child = client.post(f"/images/{gen['id']}/edit", data={"prompt": "make it blue"},
                                    files={"mask": ("mask.png", png_mask(gen["width"], gen["height"]),
                                                     "image/png")}).json()
        for _ in range(200):
            if client.get(f"/images/{private_child['id']}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.02)

        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["images"]}).json()
        auth = {"Authorization": f"Bearer {app['key']}"}
        assert client.get(f"/api/v1/images/{uid}", headers=auth).status_code == 404
        assert client.get(f"/api/v1/images/{eid}.png", headers=auth).status_code == 404
        assert client.get(f"/api/v1/images/{gen['id']}.png", headers=auth).status_code == 200
        app_gen = client.get(f"/api/v1/images/{gen['id']}", headers=auth).json()
        assert private_child["id"] not in {child["id"] for child in app_gen["children"]}
        assert client.post(f"/api/v1/images/{uid}/upscale", headers=auth,
                           json={"upscale": "2x"}).status_code == 404


def test_queue_hold_progress_cancel_restart_failure_delete_backup(tmp_path):
    async def body():
        from types import SimpleNamespace

        m, server, _ = edit_manager(tmp_path)
        await m.start(maintenance=False)
        parent = m.images.ingest_upload(png_rgb())
        queued = m.images.submit_edit(parent["id"], "queued edit", png_mask())
        m.images.cancel(queued["id"])
        cancelled = await m.images.wait(queued["id"])
        assert cancelled["status"] == "cancelled"
        assert m.images.source_path(cancelled).exists() and m.images.mask_path(cancelled).exists()

        m.images.apply_comfy_progress("edit1", time.time() - 1, {"type": "progress", "data": {"value": 2, "max": 50}})
        assert m.images.status()["progress"]["max"] == 50

        guard = SimpleNamespace(active=False, manual=True)
        m.runner.guard = guard
        held = m.images.submit_edit(parent["id"], "held edit", png_mask())
        for _ in range(40):
            if m.images.phase == "waiting":
                break
            await asyncio.sleep(0.01)
        assert m.images.phase == "waiting"
        guard.manual = False
        m.images._guard_wake.set()
        done = await m.images.wait(held["id"])
        assert done["status"] == "done"

        m2, _, _ = edit_manager(tmp_path / "restart")
        parent2 = m2.images.ingest_upload(png_rgb())
        restart_id = "restartjob01"
        restart_job = {"id": restart_id, "session_id": "", "source": "phone", "prompt": "restart me",
                       "model": image_edit.EDIT_MODEL_ID, "aspect_ratio": "1:1", "resolution": "upload",
                       "width": parent2["width"], "height": parent2["height"], "seed": 1,
                       "parent_id": parent2["id"], "operation": image_edit.OPERATION_EDIT,
                       "model_revision": image_edit.EDIT_MODEL["revision"], "feather": 0}
        m2.images.images_dir.mkdir(parents=True, exist_ok=True)
        m2.images.source_path(restart_job).write_bytes(m2.images.path(parent2).read_bytes())
        m2.images.mask_path(restart_job).write_bytes(png_mask(parent2["width"], parent2["height"]))
        m2.db.insert_image(restart_job)
        m2.db.update_image(restart_id, status="running")
        await m2.start(maintenance=False)
        recovered = await m2.images.wait(restart_id)
        assert recovered["status"] == "done"

        fail_m, _, fail_state = image_manager(tmp_path / "fail", fail_prompts=("broken edit",))
        stub_edit_assets(tmp_path / "fail", fail_m.images.cfg)
        parent3 = fail_m.images.ingest_upload(png_rgb())
        failed = fail_m.images.submit_edit(parent3["id"], "broken edit", png_mask())
        await fail_m.start(maintenance=False)
        failed = await fail_m.images.wait(failed["id"])
        assert failed["status"] == "failed"
        assert fail_m.images.source_path(failed).exists()
        await fail_m.stop()

        backup = tmp_path / "backups" / "2026-09-17"
        backup.mkdir(parents=True)
        live = m.images.path(done)
        archived = backup / live.name
        archived.write_bytes(live.read_bytes())
        m.images.delete(done["id"], backup_dir=tmp_path / "backups")
        assert m.db.get_image(done["id"]) is None
        assert not live.exists()
        assert archived.exists()
        assert m.db.get_image(parent["id"]) is not None
        await m.stop()
        await m2.stop()
    asyncio.run(body())


def test_web_mask_editor_round_trips_source_pixels():
    js = (Path(__file__).parents[1] / "harness" / "web" / "app.js").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "harness" / "web" / "style.css").read_text(encoding="utf-8")
    client = (Path(__file__).parents[1] / "harness" / "web" / "client.mjs").read_text(encoding="utf-8")
    assert "function maskEditor" in js and "viewImageEdit" in js
    assert "canvas.width / r.width" in js
    assert 'accept: "image/png,image/jpeg,image/webp,image/jpg"' in js
    assert "Independent backups are not changed" in js
    assert "White is edited, black is preserved" in js
    assert "body instanceof FormData" in client
    assert "touch-action: none" in css
    assert "@media (max-width: 640px)" in css and ".mask-tools" in css
