"""Issue #86: generated PNGs have one durable archive outside dated snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import BackupConfig, ImagesConfig
from harness.image_archive import ImageArchiveError
from harness.llm import Completion
from harness.manager import Manager
from harness.metrics import render

from test_daemon import Script, make_cfg

PNG = b"\x89PNG\r\n\x1a\narchive-test"


def archive_manager(tmp_path: Path, *, enabled: bool = True) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=enabled, dir=str(tmp_path / "backups"), image_archive_min_free_gb=0)
    cfg.images = ImagesConfig(enabled=enabled, work_dir=str(tmp_path / "image-work"))
    return Manager(cfg, chat=Script([Completion(content="done")]))


def completed_image(m: Manager, iid: str = "abc123", *, created_at: float | None = None,
                    content: bytes = PNG, write_source: bool = True) -> dict:
    job = {"id": iid, "session_id": "", "source": "phone", "prompt": "a small blue house",
           "model": "fast", "aspect_ratio": "1:1", "resolution": "standard", "width": 1024,
           "height": 1024, "seed": 42}
    m.db.insert_image(job)
    if created_at is not None:
        m.db.update_image(iid, created_at=created_at)
    m.db.update_image(iid, status="done", finished_at=time.time(), bytes=len(content))
    saved = m.db.get_image(iid)
    if write_source:
        source = m.image_archive.source(saved)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    return m.db.get_image(iid)


def test_archive_success_metadata_idempotency_and_interrupted_partial(tmp_path):
    m = archive_manager(tmp_path)
    job = completed_image(m, created_at=1758067200)  # 2025-09-17 UTC
    first = m.image_archive.archive(job)
    png, sidecar = m.image_archive.paths(job)
    assert png.relative_to(tmp_path / "backups").as_posix() == "images/2025/09/abc123.png"
    assert png.read_bytes() == PNG
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta == {
        "job_id": "abc123", "created_at": "2025-09-17T00:00:00Z", "source": "phone",
        "model": "fast", "workflow": "fast", "prompt": "a small blue house",
        "dimensions": {"width": 1024, "height": 1024}, "seed": 42, "bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    row = m.db.get_image("abc123")
    assert row["archived_at"]
    assert row["archive_bytes"] == len(PNG)
    assert row["sha256"] == first["sha256"]
    png_mtime, metadata_mtime, archived_at = png.stat().st_mtime_ns, sidecar.stat().st_mtime_ns, row["archived_at"]
    assert m.image_archive.archive(row)["sha256"] == first["sha256"]
    assert (png.stat().st_mtime_ns, sidecar.stat().st_mtime_ns, m.db.get_image("abc123")["archived_at"]) == (
        png_mtime, metadata_mtime, archived_at)

    png.unlink()
    png.with_name(png.name + ".partial").write_bytes(b"interrupted")
    report = m.image_archive.reconcile()
    assert report["archived"] == 1
    assert report["missing"] == report["errors"] == 0
    assert png.read_bytes() == PNG
    assert not png.with_name(png.name + ".partial").exists()


def test_reconcile_repairs_corruption_and_reports_hash_mismatch_and_missing_source(tmp_path):
    m = archive_manager(tmp_path)
    first = completed_image(m, "first")
    second = completed_image(m, "second")
    third = completed_image(m, "third", write_source=False)
    m.image_archive.archive(first)
    m.image_archive.archive(second)
    png, _ = m.image_archive.paths(first)
    png.write_bytes(b"corrupt")
    # The recorded source hash makes later gallery corruption distinguishable from a safe repair.
    second_png, second_sidecar = m.image_archive.paths(second)
    second_png.unlink()
    second_sidecar.unlink()
    m.image_archive.source(second).write_bytes(PNG[:-1] + b"X")

    report = m.image_archive.reconcile()
    assert png.read_bytes() == PNG
    assert report["archived"] == 1
    assert report["missing"] == 2
    assert report["errors"] == 2
    assert "SHA-256" in m.db.get_image("second")["archive_error"]
    assert "missing" in m.db.get_image("third")["archive_error"]
    assert any("second" in warning for warning in report["warnings"])


def test_unwritable_and_low_space_are_actionable_without_breaking_snapshot(tmp_path, monkeypatch):
    m = archive_manager(tmp_path)
    completed_image(m, "missing", write_source=False)
    monkeypatch.setattr(m.image_archive, "_free_space", lambda: {
        "free_bytes": 12, "free_space_warning": "image archive has less than the configured free-space threshold"})

    async def run():
        result = await m.maintenance.backup()
        assert Path(result["path"], "harness.sqlite3").is_file()
        assert result["image_archive"]["errors"] == 1
        assert result["image_archive"]["free_space_warning"]
        assert m.maintenance.last_backup["ok_at"]
    asyncio.run(run())

    original_root = m.image_archive.root
    m.image_archive.root = tmp_path / "not-writable"
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("access denied")))
    report = m.image_archive.reconcile()
    assert report["errors"] == 1
    assert any("not writable" in warning for warning in report["warnings"])
    m.image_archive.root = original_root


def test_retention_requires_preview_and_never_deletes_live_gallery(tmp_path):
    m = archive_manager(tmp_path)
    m.cfg.backup.image_archive_keep_days = 30
    old = completed_image(m, "old", created_at=1000)
    recent = completed_image(m, "recent", created_at=time.time())
    m.image_archive.archive(old)
    m.image_archive.archive(recent)
    old_source = m.image_archive.source(old)
    preview = m.image_archive.retention_preview(now=1000 + 31 * 86400)
    assert preview["count"] == 1
    assert preview["bytes"] == len(PNG)
    assert preview["confirmation"]
    with pytest.raises(ImageArchiveError, match="preview"):
        m.image_archive.apply_retention("wrong", now=1000 + 31 * 86400)
    applied = m.image_archive.apply_retention(preview["confirmation"], now=1000 + 31 * 86400)
    assert applied == {"removed": 1, "bytes": len(PNG), "errors": []}
    assert old_source.is_file()
    assert not m.image_archive.paths(old)[0].exists()
    assert m.image_archive.paths(recent)[0].exists()
    assert m.db.get_image("old")["archive_deleted_at"] is not None
    # Reconciliation honors an explicit retention deletion instead of silently restoring it.
    assert m.image_archive.reconcile()["archived"] == 1


def test_retention_defaults_indefinite(tmp_path):
    m = archive_manager(tmp_path)
    old = completed_image(m, created_at=1000)
    m.image_archive.archive(old)
    assert m.image_archive.retention_preview(now=time.time())["count"] == 0
    with pytest.raises(ImageArchiveError, match="preview"):
        m.image_archive.apply_retention("")


def test_archive_health_metrics_authorization_and_disabled_modules(tmp_path):
    m = archive_manager(tmp_path)
    m.cfg.allowed_logins = ["owner@example.test"]
    job = completed_image(m)
    m.image_archive.archive(job)
    m.image_archive.reconcile()
    metrics = render(m)
    assert "harness_image_archive_images{state=\"archived\"} 1" in metrics
    assert f"harness_image_archive_bytes {len(PNG)}" in metrics
    assert str(tmp_path) not in metrics

    with TestClient(create_app(m)) as client:
        usage = client.get("/maintenance").json()["image_archive"]
        assert usage["archived"] == 1
        assert usage["path"].endswith("images")
        assert client.post("/maintenance/image-archive/retention/preview",
                           headers={"Tailscale-User-Login": "guest@example.test"}).status_code == 403
        app_key = client.post("/keys", json={"name": "app", "kind": "app", "scopes": ["images"]}).json()["key"]
        assert client.get("/maintenance", headers={"Authorization": f"Bearer {app_key}"}).status_code == 403
        refused = client.post("/api/admin/v1/maintenance/image-archive/retention/preview",
                              headers={"Authorization": f"Bearer {app_key}"})
        assert refused.status_code == 403

    disabled = archive_manager(tmp_path / "disabled", enabled=False)
    assert disabled.image_archive.health() == {"enabled": False, "last_reconciliation": 0, "archived": 0,
                                               "missing": 0, "errors": 0, "bytes": 0, "retained": 0}
    assert "harness_image_archive_bytes" not in render(disabled)
    assert disabled.image_archive.archive({}) == {"archived": False, "disabled": True}


def test_generated_image_is_archived_before_done(tmp_path):
    from test_phase6 import FakeServer, fake_comfy

    m = archive_manager(tmp_path)
    server = FakeServer()
    m.images.control = server
    handler, _ = fake_comfy()
    import httpx
    m.images.transport = httpx.MockTransport(handler)

    async def no_process():
        return None
    m.images.comfy.start = no_process
    m.images.comfy.stop = no_process

    async def run():
        await m.start(maintenance=False)
        submitted = m.images.submit("archive me")
        done = await m.images.wait(submitted["id"])
        assert done["status"] == "done"
        assert done["archived_at"]
        assert done["sha256"]
        assert m.image_archive.paths(done)[0].read_bytes().startswith(b"\x89PNG")
        await m.stop()
    asyncio.run(run())
