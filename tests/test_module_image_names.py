"""Stage (c) image boundaries, authorization and durable storage compatibility (#334)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import access, modules
from harness.api import create_app
from harness.checkpoints import MUTATING_TOOLS
from harness.db import Database
from harness_modules.images.store import ImageStore

from test_modules import images_manager


@pytest.mark.parametrize("packages,enabled,present", [
    (["harness_modules.images"], True, True),
    (["harness_modules.images"], False, True),
    ([], True, False),
])
def test_image_contributions_present_disabled_absent(tmp_path, packages, enabled, present):
    m = images_manager(tmp_path, packages=packages, enabled=enabled)
    member = SimpleNamespace(role="member", allowed=True)
    for path in ("/images", "/images/abc", "/api/v1/images/abc"):
        assert access.member_forbidden(member, "GET", path, m.cfg) == (
            "members cannot use image generation" if present else None)
    assert access.member_forbidden(member, "GET", "/images-unrelated", m.cfg) is None
    assert modules.member_forbidden(m.cfg, "/other") is None
    assert "generate_image" not in MUTATING_TOOLS
    assert ("generate_image" in m.modules.mutating_tools()) is present
    assert m.modules.gpu_holders() == []
    if present:
        rt = m.modules.get("images")
        assert (rt.service is not None) is enabled
        rt.service = SimpleNamespace(gpu_taken=True)
        assert m.modules.gpu_holders() == ["ComfyUI"]
        rt.service = None
        assert m.modules.features()["images"] is False
    else:
        assert "images" not in m.modules.features()
    m.db.close()


@pytest.mark.parametrize("packages", [["harness_modules.images", "harness_modules.endpoint"],
                                     ["harness_modules.endpoint"]])
def test_endpoint_uses_present_module_features(tmp_path, packages):
    m = images_manager(tmp_path, packages=packages)
    m.cfg.endpoint.enabled = True
    with TestClient(create_app(m)) as client:
        key = client.post("/keys", json={"name": "fake"}).json()["key"]
        response = client.get("/v1/capabilities", headers={"Authorization": "Bearer " + key})
        assert response.status_code == 200
        features = response.json()["features"]
        if "harness_modules.images" in packages:
            assert features["images"] is True and features["image_upscale"] is True
        else:
            assert "images" not in features and "image_upscale" not in features


def job(iid, **fields):
    return {"id": iid, "source": "phone", "prompt": "lamp", "model": "fast",
            "aspect_ratio": "1:1", "width": 32, "height": 32, "seed": 1, **fields}


def test_image_store_transactions_filters_and_legacy_rows(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    store = ImageStore(db)
    try:
        store.insert_image(job("parent", status="done", provenance={"seed": 1}, created_at=1))
        store.insert_image(job("child", parent_id="parent", operation="upscale", scale=2,
                               requested_upscale="2x", created_at=2))
        assert store.get_image("parent")["provenance"] == {"seed": 1}
        assert [j["id"] for j in store.list_images(status=("queued",), operations=("upscale",))] == ["child"]
        assert [j["id"] for j in store.list_images(limit=1)] == ["child"]
        assert store.image_children("parent")[0]["id"] == "child"
        assert store.find_image_upscale("parent", "2")["id"] == "child"
        assert store.find_image_upscale("parent", "4x") is None
        assert store.find_image_upscale("parent", "none") is None
        assert store.images_for_archive()[0]["id"] == "parent"
        store.update_image("parent", provenance={"model": "fast"})
        assert store.get_image("parent")["provenance"] == {"model": "fast"}
        store.update_image("parent", provenance="invalid")
        assert store.get_image("parent")["provenance"] == {}
        store.update_image("parent", provenance="")
        assert store.get_image("parent")["provenance"] == {}
        assert store._image_row({"provenance": None}) == {"provenance": {}}
        assert store._image_row({"provenance": {"seed": 1}}) == {"provenance": {"seed": 1}}
        assert store.get_image("missing") is None

        def failing_transaction():
            store.insert_image(job("rollback"))
            raise ValueError("rollback")

        with pytest.raises(ValueError, match="rollback"):
            db.write(failing_transaction)
        assert store.get_image("rollback") is None
        assert store.delete_image("child") and not store.delete_image("child")
    finally:
        db.close()
    # The same shared file is readable after restart without altering its baseline.
    reopened = Database(tmp_path / "state.sqlite")
    try:
        assert ImageStore(reopened).get_image("parent")["status"] == "done"
        assert not hasattr(reopened, "get_image")
    finally:
        reopened.close()


def test_image_store_async_io_uses_core_writer_and_readers(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    store = ImageStore(db)

    async def body():
        await store.aio.insert_image(job("async"))
        await store.aio.update_image("async", status="done")
        assert (await store.aio.get_image("async"))["status"] == "done"
        assert await store.aio.delete_image("async")

    try:
        asyncio.run(body())
    finally:
        db.close()
