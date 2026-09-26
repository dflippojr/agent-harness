"""Issue #88: optional masked inpainting / photo editing with Qwen-Image-Edit."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from harness import config, image_edit, setup_config
from harness.api import create_app
from harness.accounts import AccountService
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
    m.images.edit_enabled = True
    return m, server, state


def seed_done_image(m, *, iid="aaaaaaaaaaaa", width=64, height=64, operation="generate"):
    job = {"id": iid, "session_id": "", "source": "phone", "prompt": "seed",
           "model": "fast", "aspect_ratio": "1:1", "resolution": "standard",
           "width": width, "height": height, "seed": 1, "parent_id": "",
           "operation": operation, "status": "done", "created_at": 1, "scale": 4 if operation == "upscale" else 1}
    m.db.insert_image(job)
    m.images.images_dir.mkdir(parents=True, exist_ok=True)
    m.images.path(job).write_bytes(png_rgb())
    return m.db.get_image(iid)


def test_normalize_rejects_unsupported_and_empty_mask():
    with pytest.raises(ToolError, match="malformed"):
        image_edit.normalize_source(b"not-an-image")
    gif = Image.new("RGB", (16, 16), (1, 2, 3))
    buf = io.BytesIO()
    gif.save(buf, format="GIF")
    gif_bytes = buf.getvalue()
    with pytest.raises(ToolError, match="unsupported"):
        image_edit.normalize_source(gif_bytes)
    png, width, height = image_edit.normalize_source(png_rgb())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert width == 64
    assert height == 64
    empty_mask = png_mask(empty=True)
    with pytest.raises(ToolError, match="empty"):
        image_edit.normalize_mask(empty_mask, 64, 64)
    small_mask = png_mask(32, 32)
    with pytest.raises(ToolError, match="does not match"):
        image_edit.normalize_mask(small_mask, 64, 64)
    source_64 = png_rgb(64, 64)
    with pytest.raises(ToolError, match="pixel cap"):
        image_edit.normalize_source(source_64, max_pixels=10)


def test_normalize_strips_exif_and_ignores_client_path_bytes():
    data, width, height = image_edit.normalize_source(jpeg_with_exif())
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG"
    assert 274 not in image.getexif()
    assert image.mode == "RGB"
    # 80×48 with orientation 6 becomes 48×80, then floored to a multiple of 16.
    assert width % 16 == 0
    assert height % 16 == 0
    assert b"Exif" not in data


def test_full_profile_image_edit_upgrade_without_force(tmp_path):
    args = ["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"), "--model", "gpt-oss",
            "--pause-flag", str(tmp_path / "paused")]
    assert setup_config.main(args) == 0
    cfg = config.load(tmp_path / "cfg")
    assert not cfg.modules.image_edit
    assert not cfg.images.edit_enabled
    harness_path = tmp_path / "cfg" / "harness.yaml"
    original_harness = harness_path.read_text(encoding="utf-8")
    assert "edit_enabled" not in (yaml.safe_load(original_harness)["images"])

    # This is the install.ps1 -EnableModules image_edit upgrade path: setup_config
    # keeps harness.yaml without --force but refreshes the profile overlay.
    assert setup_config.main(args + ["--enable-module", "image_edit"]) == 0
    assert harness_path.read_text(encoding="utf-8") == original_harness
    enabled = config.load(tmp_path / "cfg")
    assert enabled.modules.image_edit
    assert enabled.images.edit_enabled
    assert enabled.modules.images


def test_setup_config_records_non_default_images_models_dir(tmp_path):
    from harness.config import DEFAULT_IMAGES_MODELS_DIR, resolve_images_models_dir
    from harness.images_models import models_dir as flux_models_dir

    custom = tmp_path / "opt" / "comfy-models"
    args = ["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"), "--model", "gpt-oss",
            "--pause-flag", str(tmp_path / "paused"), "--enable-module", "image_edit",
            "--images-models-dir", str(custom)]
    assert setup_config.main(args) == 0
    cfg = config.load(tmp_path / "cfg")
    assert Path(cfg.images.models_dir).resolve() == custom.resolve()
    assert image_edit.models_dir(cfg.images).resolve() == custom.resolve()
    assert flux_models_dir(cfg.images).resolve() == custom.resolve()
    assert resolve_images_models_dir(cfg.images).resolve() == custom.resolve()
    yaml_text = (tmp_path / "cfg" / "harness.yaml").read_text(encoding="utf-8")
    assert "models_dir:" in yaml_text
    assert DEFAULT_IMAGES_MODELS_DIR not in yaml_text.split("images:", 1)[-1]

    default_dir = tmp_path / "cfg-default"
    setup_config.main(["--config-dir", str(default_dir), "--data-dir", str(tmp_path / "data2"), "--model", "gpt-oss",
                       "--pause-flag", str(tmp_path / "paused"), "--enable-module", "image_edit",
                       "--images-models-dir", DEFAULT_IMAGES_MODELS_DIR])
    default_cfg = config.load(default_dir)
    assert default_cfg.images.models_dir == DEFAULT_IMAGES_MODELS_DIR
    default_yaml = (default_dir / "harness.yaml").read_text(encoding="utf-8")
    assert "models_dir" not in default_yaml


def test_edit_status_is_setup_guidance_without_assets(tmp_path):
    m, _, _ = image_manager(tmp_path)
    m.images.cfg.models_dir = str(tmp_path / "missing-models")
    status = m.images.status()["edit"]
    assert status["available"] is False
    assert status["enabled"] is False
    assert "Qwen-Image-Edit" in status["setup"]
    assert "Apache 2.0" in status["setup"]
    assert status["revision"] == image_edit.EDIT_MODEL["revision"]
    assert status["sha256"] == image_edit.EDIT_MODEL["unet"]["sha256"]
    assert not any(sep in status["setup"] for sep in ("C:\\", "/home/", "tailnet"))


def test_routine_status_does_not_hash_twenty_gb_checkpoint(tmp_path, monkeypatch):
    checkpoint = tmp_path / "edit.safetensors"
    checkpoint.write_bytes(b"stub-unet")
    monkeypatch.setitem(image_edit.EDIT_MODEL["unet"], "bytes", checkpoint.stat().st_size)
    calls = []
    monkeypatch.setattr(image_edit, "file_sha256", lambda path: calls.append(path) or image_edit.EDIT_MODEL["unet"]["sha256"])

    assert image_edit.unet_hash_ok(checkpoint) is None
    assert calls == []
    assert image_edit.unet_hash_ok(checkpoint, verify_hash=True) is True
    assert calls == [checkpoint]


def test_cold_edit_hash_runs_in_background_and_is_cached(tmp_path, monkeypatch):
    async def body():
        m, _, _ = edit_manager(tmp_path)
        checkpoint = image_edit.locate_assets(m.images.cfg)["unet"]
        monkeypatch.setitem(image_edit.EDIT_MODEL["unet"], "bytes", checkpoint.stat().st_size)
        image_edit.clear_hash_cache()
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_hash(path):
            calls.append(path)
            started.set()
            release.wait(2)
            return image_edit.EDIT_MODEL["unet"]["sha256"]

        monkeypatch.setattr(image_edit, "file_sha256", slow_hash)
        before = time.monotonic()
        status = m.images.status()["edit"]
        assert time.monotonic() - before < 1.5  # a blocking hash waits the full 2 s; generous for loaded runners
        assert status["verifying"] is True
        assert status["available"] is False
        assert started.wait(1)
        assert m.images._edit_verify_in_flight
        release.set()
        for _ in range(100):
            if m.images.status()["edit"]["available"]:
                break
            await asyncio.sleep(0.01)
        assert m.images.status()["edit"]["available"] is True
        assert calls == [checkpoint]
        assert m.images.status()["edit"]["available"] is True
        assert calls == [checkpoint]

    asyncio.run(body())


def test_remove_edit_assets_keeps_shared_files_and_parts(tmp_path):
    m, _, _ = edit_manager(tmp_path)
    files = image_edit.locate_assets(m.images.cfg)
    unet_part = files["unet"].with_name(files["unet"].name + ".part")
    clip_part = files["clip"].with_name(files["clip"].name + ".part")
    unet_part.write_bytes(b"edit partial")
    clip_part.write_bytes(b"shared partial")
    result = image_edit.remove_assets(m.images.cfg)
    assert not files["unet"].exists()
    assert not unet_part.exists()
    assert files["clip"].exists()
    assert clip_part.exists()
    assert files["vae"].exists()
    assert str(files["clip"]) in result["skipped"]
    assert str(clip_part) in result["skipped"]


def test_gallery_and_upload_edits_preserve_source(tmp_path):
    async def body():
        m, _, state = edit_manager(tmp_path)
        await m.start(maintenance=False)
        parent = m.images.submit("a red cube", model="fast")
        parent = await m.images.wait(parent["id"])
        original = m.images.path(parent).read_bytes()
        uploaded = m.images.ingest_upload(png_rgb(96, 64, (1, 2, 3)))
        assert uploaded["operation"] == "upload"
        assert uploaded["status"] == "done"
        assert uploaded["width"] == 96
        assert uploaded["height"] == 64
        gallery_edit = m.images.submit_edit(parent["id"], "replace the cube with a sphere", png_mask(parent["width"], parent["height"]))
        upload_edit = m.images.submit_edit(uploaded["id"], "add a blue sky", png_mask(uploaded["width"], uploaded["height"]))
        done = [await m.images.wait(j["id"]) for j in (gallery_edit, upload_edit)]
        assert [j["status"] for j in done] == ["done", "done"]
        assert done[0]["parent_id"] == parent["id"]
        assert done[1]["parent_id"] == uploaded["id"]
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


def test_edit_envelope_matches_upload_and_rejects_gallery_upscale(tmp_path):
    m, server, _ = edit_manager(tmp_path)
    bound = image_edit.edit_eligibility(1664, 928, max_pixels=m.images.cfg.max_pixels)
    over_side = image_edit.edit_eligibility(1665, 928, max_pixels=m.images.cfg.max_pixels)
    under_pixels = image_edit.edit_eligibility(4096, 4096, max_pixels=m.images.cfg.max_pixels)
    over_pixels = image_edit.edit_eligibility(6656, 3712, max_pixels=m.images.cfg.max_pixels)
    assert bound["editable"] is True
    assert bound["max_side"] == image_edit.MAX_EDIT_SIDE
    assert over_side["editable"] is False
    assert "too large to edit" in over_side["reason"]
    assert under_pixels["editable"] is False
    assert under_pixels["reason"]
    assert 4096 * 4096 < m.images.cfg.max_pixels
    assert over_pixels["editable"] is False
    assert 6656 * 3712 > m.images.cfg.max_pixels

    uploaded = m.images.ingest_upload(png_rgb(2000, 1000))
    assert max(uploaded["width"], uploaded["height"]) <= image_edit.MAX_EDIT_SIDE
    assert uploaded["width"] * uploaded["height"] <= m.images.cfg.max_pixels
    assert image_edit.edit_eligibility(
        uploaded["width"], uploaded["height"], max_pixels=m.images.cfg.max_pixels)["editable"]

    ok = seed_done_image(m, iid="bbbbbbbbbbbb", width=1664, height=928)
    queued = m.images.submit_edit(ok["id"], "keep the sky", png_mask(1664, 928))
    assert queued["status"] == "queued"
    assert queued["width"] == 1664

    rejected = seed_done_image(m, iid="cccccccccccc", width=1665, height=928)
    rejected_mask = png_mask(1665, 928)
    with pytest.raises(ToolError, match="too large to edit"):
        m.images.submit_edit(rejected["id"], "keep the sky", rejected_mask)

    upscale = seed_done_image(m, iid="dddddddddddd", width=4096, height=4096, operation="upscale")
    with pytest.raises(ToolError, match="non-upscaled"):
        m.images.submit_edit(upscale["id"], "replace the cube", b"not-a-mask-at-all")
    assert server.calls == []

    m.images.cfg.max_pixels = 10
    ok_mask = png_mask(1664, 928)
    with pytest.raises(ToolError, match="too large to edit"):
        m.images.submit_edit(ok["id"], "still too many pixels", ok_mask)
    upload_64 = png_rgb(64, 64)
    with pytest.raises(ToolError, match="pixel cap"):
        m.images.ingest_upload(upload_64)


def test_edit_validation_and_path_tricks_before_gpu(tmp_path):
    m, server, _ = edit_manager(tmp_path)
    parent = m.images.ingest_upload(png_rgb())
    mask = png_mask()
    with pytest.raises(ToolError, match="prompt is empty"):
        m.images.submit_edit(parent["id"], "  ", mask)
    empty_mask = png_mask(empty=True)
    with pytest.raises(ToolError, match="empty"):
        m.images.submit_edit(parent["id"], "fix the sky", empty_mask)
    small_mask = png_mask(32, 32)
    with pytest.raises(ToolError, match="does not match"):
        m.images.submit_edit(parent["id"], "fix the sky", small_mask)
    with pytest.raises(ToolError, match="malformed"):
        m.images.ingest_upload(b"MZ-not-an-image")
    # A client-supplied path is ignored; only the bytes matter.
    job = m.images.ingest_upload(png_rgb(color=(9, 9, 9)))
    assert ".." not in job["id"]
    assert m.images.path(job).parent == m.images.images_dir
    assert server.calls == []


def test_image_paths_reject_database_id_traversal(tmp_path):
    m, _, _ = edit_manager(tmp_path)
    escaped = m.images.images_dir.parent / "outside.png"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_bytes(png_rgb())
    parent = {"id": "../outside", "session_id": "", "source": "owner", "prompt": "malicious row",
              "model": image_edit.EDIT_MODEL_ID, "aspect_ratio": "1:1", "resolution": "upload",
              "width": 64, "height": 64, "seed": 0, "parent_id": "",
              "operation": image_edit.OPERATION_UPLOAD, "status": "done", "created_at": 1}
    m.db.insert_image(parent)

    mask = png_mask()
    with pytest.raises(ToolError, match="invalid image id"):
        m.images.submit_edit(parent["id"], "must not escape", mask)
    for builder in (m.images.path, m.images.source_path, m.images.mask_path):
        with pytest.raises(ToolError, match="invalid image id"):
            builder(parent)
    assert escaped.read_bytes() == png_rgb()


def test_edit_missing_assets_do_not_break_text_to_image(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path)
        await m.start(maintenance=False)
        upload = png_rgb()
        with pytest.raises(ToolError, match="image-edit component"):
            m.images.ingest_upload(upload)
        job = await m.images.wait(m.images.submit("still works")["id"])
        assert job["status"] == "done"
        assert job["operation"] == "generate"
        await m.stop()
    asyncio.run(body())


def test_image_payload_exposes_editable_flag(tmp_path):
    m, _, _ = edit_manager(tmp_path)
    m.cfg.allowed_logins = [LOGIN]
    ok = seed_done_image(m, iid="eeeeeeeeeeee", width=1664, height=928)
    huge = seed_done_image(m, iid="ffffffffffff", width=4096, height=4096, operation="upscale")
    with TestClient(create_app(m)) as client:
        ready = client.get(f"/images/{ok['id']}")
        blocked = client.get(f"/images/{huge['id']}")
        assert ready.status_code == 200
        assert ready.json()["editable"] is True
        assert ready.json()["editable_reason"] == ""
        assert blocked.status_code == 200
        assert blocked.json()["editable"] is False
        assert "too large to edit" in blocked.json()["editable_reason"]
        refused = client.post(f"/images/{huge['id']}/edit", data={"prompt": "make it dusk"},
                              files={"mask": ("mask.png", b"not-a-mask", "image/png")})
        assert refused.status_code == 400
        assert "too large to edit" in refused.json()["detail"]
        assert "malformed" not in refused.json()["detail"]


def test_image_payload_editable_false_when_edit_assets_unavailable(tmp_path):
    m, _, _ = image_manager(tmp_path)
    m.cfg.allowed_logins = [LOGIN]
    m.images.edit_enabled = True
    m.images.cfg.models_dir = str(tmp_path / "missing-models")
    ok = seed_done_image(m, iid="eeeeeeeeeeee", width=1664, height=928)
    with TestClient(create_app(m)) as client:
        payload = client.get(f"/images/{ok['id']}")
        assert payload.status_code == 200
        body = payload.json()
        assert body["service"]["edit"]["available"] is False
        assert body["editable"] is False
        assert body["editable_reason"]
        refused = client.post("/images/uploads", files={"file": ("photo.png", png_rgb(), "image/png")})
        assert refused.status_code == 400
        assert refused.json()["detail"] == body["editable_reason"]


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


def test_guest_gallery_limit_is_applied_after_private_filter(tmp_path):
    m, _, _ = edit_manager(tmp_path)
    m.cfg.allowed_logins = [LOGIN]
    m.cfg.guests = [GuestAccess(login="buddy@example.com")]

    def row(iid, operation, created):
        return {"id": iid, "session_id": "", "source": "owner", "prompt": iid,
                "model": "fast", "aspect_ratio": "1:1", "resolution": "standard",
                "width": 64, "height": 64, "seed": created, "parent_id": "",
                "operation": operation, "status": "done", "created_at": created}

    for image in (row("public-old01", "generate", 1), row("public-old02", "generate", 2),
                  row("private-new1", "upload", 3), row("private-new2", "edit", 4),
                  row("private-new3", "upload", 5)):
        m.db.insert_image(image)

    with TestClient(create_app(m)) as client:
        guest = client.get("/images?limit=2", headers={"Tailscale-User-Login": "buddy@example.com"})
        assert guest.status_code == 200
        assert [image["id"] for image in guest.json()["images"]] == ["public-old02", "public-old01"]


def test_delete_queued_image_makes_wait_return_deleted(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path)
        job = m.images.submit("delete before the worker starts")
        result = await m.images.delete(job["id"])
        assert result == {"deleted": job["id"], "parent_id": ""}
        assert m.db.get_image(job["id"]) is None
        assert await m.images.wait(job["id"]) == {
            "id": job["id"], "status": "deleted", "error": "image was deleted"}

    asyncio.run(body())


def test_delete_running_image_waits_for_worker_before_removing_files(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_generate(job, started_at):
            started.set()
            await release.wait()
            return png_rgb()

        async def no_interrupt():
            return None

        m.images._run_generate = slow_generate
        m.images.comfy.interrupt = no_interrupt
        await m.start(maintenance=False)
        job = m.images.submit("delete while running")
        await asyncio.wait_for(started.wait(), timeout=2)
        deleting = asyncio.create_task(m.images.delete(job["id"]))
        await asyncio.sleep(0)
        assert not deleting.done()
        release.set()
        result = await asyncio.wait_for(deleting, timeout=2)
        assert result == {"deleted": job["id"], "parent_id": ""}
        assert m.db.get_image(job["id"]) is None
        assert not m.images.path(job).exists()
        assert await m.images.wait(job["id"]) == {
            "id": job["id"], "status": "deleted", "error": "image was deleted"}
        await m.stop()

    asyncio.run(body())


def test_agent_image_tool_reports_deleted_job_without_key_error(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path)
        missing_id = "abcdef012345"
        m.images.submit = lambda *args, **kwargs: {"id": missing_id}
        with pytest.raises(ToolError, match="deleted"):
            await m.images.call("generate_image", {"prompt": "gone", "filename": "gone"},
                                workspace_root=tmp_path)

    asyncio.run(body())


def test_cancel_running_edit_records_cancelled_not_failed(tmp_path):
    async def body():
        m, _, _ = edit_manager(tmp_path)
        parent = m.images.ingest_upload(png_rgb())
        started = asyncio.Event()
        release = asyncio.Event()

        async def interrupted_edit(job, started_at):
            started.set()
            await release.wait()
            raise ToolError("cancelled")

        async def no_interrupt():
            return None

        m.images._run_edit = interrupted_edit
        m.images.comfy.interrupt = no_interrupt
        await m.start(maintenance=False)
        edit = m.images.submit_edit(parent["id"], "cancel me", png_mask())
        await asyncio.wait_for(started.wait(), timeout=2)
        cancelling = asyncio.create_task(m.images.cancel(edit["id"]))
        release.set()
        await cancelling
        cancelled = await asyncio.wait_for(m.images.wait(edit["id"]), timeout=2)
        assert cancelled["status"] == "cancelled"
        assert cancelled["error"] == "cancelled"
        await m.stop()

    asyncio.run(body())


def test_household_member_cannot_reach_any_edit_data_or_route(tmp_path):
    member_login = "member@example.com"
    m, _, _ = edit_manager(tmp_path)
    m.cfg.allowed_logins = [LOGIN]
    AccountService(m).create("owner", member_login, "Member")
    headers = {"Tailscale-User-Login": member_login}
    with TestClient(create_app(m)) as client:
        routes = [
            ("get", "/images"),
            ("post", "/images/uploads"),
            ("post", "/images/private123/edit"),
            ("post", "/images/private123/cancel"),
            ("delete", "/images/private123"),
            ("get", "/images/private123.source.png"),
            ("get", "/images/private123.mask.png"),
            ("get", "/api/admin/v1/images/private123.mask.png"),
            ("get", "/api/v1/images/private123"),
        ]
        for method, path in routes:
            response = getattr(client, method)(path, headers=headers)
            assert response.status_code == 403, (method, path, response.text)


def test_queue_hold_progress_cancel_restart_failure_delete_backup(tmp_path):
    async def body():
        from types import SimpleNamespace

        m, server, _ = edit_manager(tmp_path)
        await m.start(maintenance=False)
        parent = m.images.ingest_upload(png_rgb())
        queued = m.images.submit_edit(parent["id"], "queued edit", png_mask())
        await m.images.cancel(queued["id"])
        cancelled = await m.images.wait(queued["id"])
        assert cancelled["status"] == "cancelled"
        assert m.images.source_path(cancelled).exists()
        assert m.images.mask_path(cancelled).exists()

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
        restart_id = "abcdef012345"
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
        fail_m.images.edit_enabled = True
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
        await m.images.delete(done["id"], backup_dir=tmp_path / "backups")
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
    assert "function maskEditor" in js
    assert "viewImageEdit" in js
    assert "canvas.width / r.width" in js
    assert 'accept: "image/png,image/jpeg,image/webp,image/jpg"' in js
    assert "Independent backups are not changed" in js
    assert "White is edited, black is preserved" in js
    assert "img.editable !== false" in js
    assert "disabled: true, title: editBlockedReason" in js
    assert "img.editable === false" in js
    assert "body instanceof FormData" in client
    assert "touch-action: none" in css
    assert "@media (max-width: 640px)" in css
    assert ".mask-tools" in css
