"""Nightly transcript backups use temporary data only, including link containment."""

import asyncio
import os
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from harness import storage
from harness.config import BackupConfig
from harness.db import Database
from harness.maintenance import Maintenance
from test_daemon import make_cfg


@pytest.fixture
def maintenance(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=False, dir=str(tmp_path / "backups"), keep_days=14)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    for user_id in ("u-first", "u-second", "u-empty", "u-missing"):
        db.insert_account({"user_id": user_id, "role": "member", "login": user_id,
                           "display_name": user_id, "enabled": 0 if user_id == "u-second" else 1,
                           "disk_quota_bytes": 0, "max_running": 1, "max_queued": 1,
                           "created_at": 1, "updated_at": 1, "last_activity_at": 0})
    m = Maintenance(cfg, db, None)
    yield m
    db.close()


def seed(m):
    roots = [storage.transcripts_dir(m.cfg, "owner"),
             storage.transcripts_dir(m.cfg, "u-first"), storage.transcripts_dir(m.cfg, "u-second"),
             storage.transcripts_dir(m.cfg, "owner", "k-aaaa"),
             storage.transcripts_dir(m.cfg, "owner", "k-bbbb")]
    for i, root in enumerate(roots):
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "session.md").write_text(f"account {i}", encoding="utf-8")
        (root.parent / "artifacts").mkdir(exist_ok=True)
        (root.parent / "artifacts" / "excluded.txt").write_text("not a transcript")
    storage.transcripts_dir(m.cfg, "u-empty").mkdir(parents=True)
    storage.transcripts_dir(m.cfg, "owner", "k-empty").mkdir(parents=True)
    for root in (m.cfg.data_dir / "users" / "unknown", m.cfg.data_dir / "apps" / "not an app"):
        (root / "transcripts").mkdir(parents=True)
        (root / "transcripts" / "excluded.md").write_text("not eligible")
    return roots


def test_all_transcripts_and_retention_and_persisted_result(maintenance):
    m = maintenance
    seed(m)
    old = Path(m.cfg.backup.dir) / "2020-01-01" / "transcripts" / "users"
    old.mkdir(parents=True)
    (old / "u-first.zip").write_bytes(b"old")
    result = asyncio.run(m.backup())
    assert result["transcript_archives"] == 5
    assert result["warnings"] == []
    assert result["removed"] == ["2020-01-01"]
    assert not old.exists()
    assert Maintenance(m.cfg, m.db, None).last_backup == result
    dest = Path(result["path"])
    archives = ["transcripts.zip", "transcripts/users/u-first.zip", "transcripts/users/u-second.zip",
                "transcripts/apps/k-aaaa.zip", "transcripts/apps/k-bbbb.zip"]
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.zip")) == sorted(archives)
    for i, name in enumerate(archives):
        with zipfile.ZipFile(dest / name) as archive:
            assert archive.namelist() == ["nested/session.md"]
            assert archive.read("nested/session.md") == f"account {i}".encode()
            assert archive.getinfo("nested/session.md").compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.parametrize("target", ["u-first", "k-aaaa", "owner"])
def test_archive_write_failure(maintenance, monkeypatch, target):
    m = maintenance
    seed(m)
    original = zipfile.ZipFile.write
    def fail(archive, filename, *args, **kwargs):
        if target == "owner" and Path(archive.filename).name == "transcripts.zip":
            raise OSError("test owner failure")
        if Path(archive.filename).stem == target:
            raise OSError("test secondary failure")
        return original(archive, filename, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "write", fail)
    if target == "owner":
        with pytest.raises(OSError, match="owner failure"):
            asyncio.run(m.backup())
        assert not m.last_backup.get("ok_at")
        return
    result = asyncio.run(m.backup())
    assert result["transcript_archives"] == 4
    assert len(result["warnings"]) == 1
    assert "test secondary failure" in result["warnings"][0]
    assert not list(Path(result["path"]).rglob(f"{target}.zip"))
    assert result["error"] == ""


def junction_or_symlink(link, target):
    if os.name == "nt":
        # Junction creation needs no Developer Mode or elevated symlink privilege.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("location", ["inside", "member-root", "app-root", "apps-root", "users-root"])
def test_links_are_skipped_with_warnings(maintenance, tmp_path, location):
    m = maintenance
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside must not be backed up")
    owner = storage.transcripts_dir(m.cfg, "owner")
    owner.mkdir()
    (owner / "safe.md").write_text("safe")
    if location == "inside":
        link = owner / "linked"
    elif location == "member-root":
        link = storage.user_root(m.cfg, "u-first")
    elif location == "app-root":
        link = storage.app_root(m.cfg, "k-aaaa")
    else:
        link = m.cfg.data_dir / ("apps" if location == "apps-root" else "users")
    link.parent.mkdir(parents=True, exist_ok=True)
    # Ancestor links must also be rejected even when transcripts is an ordinary folder.
    if location != "inside":
        (outside / "transcripts").mkdir()
        (outside / "transcripts" / "secret.md").write_text("outside")
    junction_or_symlink(link, outside)
    try:
        result = m._backup_sync(time.time())
        assert result["transcript_archives"] == 1
        assert result["warnings"] and any(str(link) in warning for warning in result["warnings"])
        with zipfile.ZipFile(Path(result["path"]) / "transcripts.zip") as archive:
            assert archive.namelist() == ["safe.md"]
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_missing_owner_preserves_legacy_empty_archive(maintenance):
    result = maintenance._backup_sync(time.time())
    assert result["transcript_archives"] == 1
    with zipfile.ZipFile(Path(result["path"]) / "transcripts.zip") as archive:
        assert archive.namelist() == []


def test_redirect_above_configured_data_directory_is_allowed(maintenance, tmp_path):
    m = maintenance
    seed(m)
    redirect = tmp_path / "redirect"
    junction_or_symlink(redirect, tmp_path)
    original_data_dir = m.cfg.data_dir
    try:
        m.cfg.data_dir = redirect / "data"
        result = m._backup_sync(time.time())
        assert result["transcript_archives"] == 5
        assert result["warnings"] == []
        with zipfile.ZipFile(Path(result["path"]) / "transcripts/users/u-first.zip") as archive:
            assert archive.read("nested/session.md") == b"account 1"
    finally:
        m.cfg.data_dir = original_data_dir
        if os.name == "nt":
            redirect.rmdir()
        else:
            redirect.unlink()
