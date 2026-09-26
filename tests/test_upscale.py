"""Issue #87: opt-in Real-ESRGAN 2×/4× upscaling."""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from harness.fileops import ToolError
from harness import upscale as upscale_mod
from harness.api import create_app

from test_phase6 import PNG, image_manager


def rgba_png(width: int, height: int, color=(10, 20, 30, 128)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def rgb_png(width: int, height: int, color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def test_parse_choice_and_default_none():
    assert upscale_mod.parse_choice(None) == "none"
    assert upscale_mod.parse_choice("") == "none"
    assert upscale_mod.parse_choice("2×") == "2x"
    assert upscale_mod.parse_choice("4x") == "4x"
    with pytest.raises(ToolError, match="upscale must be"):
        upscale_mod.parse_choice("8x")


def test_plan_tiles_cover_and_overlap():
    tiles = upscale_mod.plan_tiles(600, 400, tile=256, overlap=32)
    assert tiles[0] == upscale_mod.Tile(0, 0, 256, 256)
    xs, ys = {t.x for t in tiles}, {t.y for t in tiles}
    assert 0 in xs
    assert 0 in ys
    assert max(t.x + t.w for t in tiles) == 600
    assert max(t.y + t.h for t in tiles) == 400
    # neighboring tiles overlap
    assert any(a.x + a.w - b.x == 32 for a in tiles for b in tiles if a.y == b.y and b.x > a.x)


def test_tiled_scale_matches_full_nearest_and_alpha_helper():
    pixels = [[(x + y, x, y) for x in range(20)] for y in range(12)]
    tiled = upscale_mod.tiled_scale(pixels, 2, tile=8, overlap=2)
    full = upscale_mod.nearest_scale(pixels, 2)
    assert tiled == full
    src = rgba_png(8, 6)
    rgb = rgb_png(16, 12)
    merged = upscale_mod.preserve_alpha(src, rgb)
    out = Image.open(io.BytesIO(merged))
    assert out.size == (16, 12)
    assert out.mode == "RGBA"
    assert out.getchannel("A").getextrema()[0] > 0
    unchanged = upscale_mod.preserve_alpha(rgb_png(8, 6), rgb)
    assert Image.open(io.BytesIO(unchanged)).mode in ("RGB", "RGBA")


def test_dimension_cap_refuses_before_allocation():
    out = upscale_mod.check_dimensions(1328, 1328, 4, max_px=36_000_000)
    assert out == (5312, 5312)
    with pytest.raises(ToolError, match="pixel cap"):
        upscale_mod.check_dimensions(5312, 5312, 4, max_px=36_000_000)


def test_missing_weights_and_hashes(tmp_path):
    from harness.config import ImagesConfig
    cfg = ImagesConfig(enabled=True, comfy_dir=str(tmp_path / "comfy"), upscale_dir=str(tmp_path / "w"))
    missing = upscale_mod.missing_weights(cfg)
    assert len(missing) == 2
    assert "RealESRGAN_x2plus.pth" in missing[0]
    assert "Image generation still works" in upscale_mod.remediation(cfg)
    (tmp_path / "w").mkdir()
    spec = upscale_mod.MODELS[2]
    (tmp_path / "w" / spec.filename).write_bytes(b"nope")
    hashed = upscale_mod.missing_weights(cfg, verify_hash=True)
    assert any("sha256" in item for item in hashed)
    status = upscale_mod.status(cfg)
    assert status["default"] == "none"
    assert status["available"] is False
    assert status["max_pixels"] == 36_000_000


def test_generate_does_not_upscale_by_default(tmp_path):
    async def body():
        m, _, state = image_manager(tmp_path, weights=True)
        await m.start(maintenance=False)
        job = await m.images.wait(m.images.submit("a red cube")["id"])
        assert job["status"] == "done"
        assert job["requested_upscale"] == "none"
        assert m.db.image_children(job["id"]) == []
        assert all(not any(n.get("class_type") == "ImageUpscaleWithModel" for n in g.values()) for g in state["graphs"])
        listing = m.images.status()
        assert listing["upscale"]["default"] == "none"
        await m.stop()
    asyncio.run(body())


def test_gallery_upscale_preserves_source_and_dimensions(tmp_path):
    async def body():
        m, server, state = image_manager(tmp_path, weights=True)
        await m.start(maintenance=False)
        parent = await m.images.wait(m.images.submit("a lighthouse", aspect_ratio="16:9")["id"])
        source = m.images.path(parent).read_bytes()
        assert source == PNG
        # replace with a real PNG so alpha merge and size checks are meaningful
        real = rgba_png(parent["width"] and 48, 32)
        m.images.path(parent).write_bytes(real)
        m.db.update_image(parent["id"], width=48, height=32)
        parent = m.db.get_image(parent["id"])
        child = m.images.submit_upscale(parent["id"], "2x")
        child = await m.images.wait(child["id"])
        assert child["status"] == "done"
        assert (child["width"], child["height"]) == (96, 64)
        assert child["parent_id"] == parent["id"]
        assert child["operation"] == "upscale"
        assert child["upscale_model"] == "RealESRGAN_x2plus"
        assert m.images.path(parent).read_bytes() == real
        out = Image.open(m.images.path(child))
        assert out.size == (96, 64)
        assert out.mode == "RGBA"
        assert any(n.get("class_type") == "ImageUpscaleWithModel" for g in state["graphs"] for n in g.values())
        again = m.images.submit_upscale(parent["id"], "2x")
        assert again["id"] == child["id"]
        await m.stop()
    asyncio.run(body())


def test_generate_plus_upscale_holds_gpu_once(tmp_path):
    async def body():
        m, server, state = image_manager(tmp_path, weights=True)
        await m.start(maintenance=False)
        job = m.images.submit("a bicycle", upscale="2x")
        gen = await m.images.wait(job["id"])
        assert gen["status"] == "done"
        assert gen["requested_upscale"] == "2x"
        child = m.db.find_image_upscale(gen["id"], "2x")
        assert child is not None
        child = await m.images.wait(child["id"])
        assert child["status"] == "done"
        assert child["width"] == gen["width"] * 2
        assert [n.get("class_type") for g in state["graphs"] for n in g.values()
                if n.get("class_type") in ("KSampler", "ImageUpscaleWithModel")] == ["KSampler", "ImageUpscaleWithModel"]
        for _ in range(200):
            if m.images.phase == "idle":
                break
            await asyncio.sleep(0.01)
        assert server.calls == ["stop", "start"]
        await m.stop()
    asyncio.run(body())


def test_failed_phone_generate_with_upscale_notifies(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path, weights=True, fail_prompts=("broken",))
        notifications = []
        m.images.notify = notifications.append
        await m.start(maintenance=False)
        job = m.images.submit("broken", source="phone", upscale="2x")
        failed = await m.images.wait(job["id"])
        assert failed["status"] == "failed"
        assert "out of memory" in failed["error"]
        assert notifications == [failed]
        assert m.db.image_children(job["id"]) == []
        await m.stop()
    asyncio.run(body())


def test_missing_weights_keep_generation_and_refuse_upscale(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path, weights=False)
        await m.start(maintenance=False)
        job = await m.images.wait(m.images.submit("ok without upscaler")["id"])
        assert job["status"] == "done"
        with pytest.raises(ToolError, match="Image generation still works"):
            m.images.submit_upscale(job["id"], "2x")
        with pytest.raises(ToolError, match="Image generation still works"):
            m.images.submit("nope", upscale="4x")
        await m.stop()
    asyncio.run(body())


def test_dimension_cap_and_oom_failure(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path, weights=True, fail_upscale=True)
        await m.start(maintenance=False)
        parent = await m.images.wait(m.images.submit("tiny")["id"])
        m.db.update_image(parent["id"], width=8000, height=8000)
        with pytest.raises(ToolError, match="pixel cap"):
            m.images.submit_upscale(parent["id"], "4x")
        m.db.update_image(parent["id"], width=48, height=32)
        m.images.path(parent).write_bytes(rgb_png(48, 32))
        child = await m.images.wait(m.images.submit_upscale(parent["id"], "2x")["id"])
        assert child["status"] == "failed"
        assert "out of memory" in child["error"]
        retried = m.images.submit_upscale(parent["id"], "2x")
        assert retried["id"] == child["id"]
        assert retried["status"] == "queued"
        await m.stop()
    asyncio.run(body())


def test_upscale_api_authorization_and_app_default(tmp_path):
    from harness.config import GuestAccess

    m, _, _ = image_manager(tmp_path, weights=True)
    login = "me@example.com"
    guest = "buddy@example.com"
    m.cfg.allowed_logins = [login]
    m.cfg.guests = [GuestAccess(login=guest, until="2099-01-01T00:00:00+00:00")]
    with TestClient(create_app(m)) as client:
        job = client.post("/images", json={"prompt": "a cat"}).json()
        for _ in range(200):
            if client.get(f"/images/{job['id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        detail = client.get(f"/images/{job['id']}").json()
        assert detail["requested_upscale"] == "none"
        assert detail["children"] == []
        assert client.post("/images", json={"prompt": "x", "upscale": "8x"}).status_code == 400
        gh = {"Tailscale-User-Login": guest}
        assert client.post(f"/images/{job['id']}/upscale", json={"upscale": "2x"}, headers=gh).status_code == 403
        owner = {"Tailscale-User-Login": login}
        m.images.path(m.db.get_image(job["id"])).write_bytes(rgb_png(32, 32))
        m.db.update_image(job["id"], width=32, height=32)
        up = client.post(f"/images/{job['id']}/upscale", json={"upscale": "2x"}, headers=owner)
        assert up.status_code == 201
        for _ in range(200):
            if client.get(f"/images/{up.json()['id']}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        app_key = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"]}).json()
        assert client.post(f"/api/v1/images/{job['id']}/upscale", json={"upscale": "2x"},
                           headers={"Authorization": f"Bearer {app_key['key']}"}).status_code == 403
        img_key = client.post("/keys", json={"name": "imager", "kind": "app", "scopes": ["images"]}).json()
        created = client.post("/api/v1/images", json={"prompt": "logo"},
                              headers={"Authorization": f"Bearer {img_key['key']}"}).json()
        assert created["requested_upscale"] == "none"


def test_agent_generate_image_upscale_saves_derived(tmp_path):
    from harness.llm import Completion
    from test_daemon import call, events, wait_status

    steps = [Completion(tool_calls=[call("generate_image", 0, prompt="app icon", filename="assets/icon",
                                         upscale="2x")]),
             Completion(content="made the icon")]

    async def body():
        m, _, _ = image_manager(tmp_path, steps=steps, weights=True)
        await m.start(maintenance=False)
        s = m.create("make an icon")
        await wait_status(m, s["id"], "done", timeout=20)
        result = events(m, s["id"], "tool_result")[0]
        assert result["ok"]
        assert "upscaled 2x" in result["output"]
        saved = Image.open(tmp_path / "data" / "workspaces" / s["id"] / "assets" / "icon.png")
        jobs = m.db.list_images()
        gen = next(j for j in jobs if j["operation"] == "generate")
        child = next(j for j in jobs if j["operation"] == "upscale")
        assert saved.size == (child["width"], child["height"])
        assert m.images.path(gen).read_bytes() == PNG
        await m.stop()
    asyncio.run(body())


def test_restart_recovers_pending_upscale(tmp_path):
    async def body():
        m, _, _ = image_manager(tmp_path, weights=True)
        await m.start(maintenance=False)
        parent = await m.images.wait(m.images.submit("recover me", upscale="2x")["id"])
        child = m.db.find_image_upscale(parent["id"], "2x")
        await m.images.wait(child["id"])
        await m.stop()
        m.db.update_image(child["id"], status="running")
        m.images._task = None
        m.images.start()
        recovered = await m.images.wait(child["id"])
        assert recovered["status"] == "done"
        await m.stop()
    asyncio.run(body())
