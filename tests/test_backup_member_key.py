"""Issue #414: synthetic credentials and temporary stores only, no daemon or provider."""

import asyncio
import base64
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.config import BackupConfig
from harness.db import Database
from harness.member_keys import MemberKeys
from harness_modules.backup import member_key, settings
from harness_modules.backup.restore import RestoreRefused, apply, plan, restore
from harness_modules.backup.runtime import doctor
from harness_modules.backup.service import BackupService
from test_daemon import make_cfg

CANARY = "synthetic-member-api-key-123456789"


@pytest.fixture
def snapshot(tmp_path, caplog):
    cfg = make_cfg(tmp_path / "source")
    cfg.port = 0
    cfg.backup = BackupConfig(dir=str(tmp_path / "backups"))
    cfg.data_dir.mkdir(parents=True)
    db = Database(cfg.db_path)
    keys = MemberKeys(cfg, db, probe=lambda *_: (True, ""))
    keys.set("u-synthetic", "codex", CANARY)
    result = asyncio.run(BackupService(cfg, db).backup())
    db.close()
    assert caplog.messages.count(member_key.WARNING) == 1
    assert CANARY not in caplog.text
    target = make_cfg(tmp_path / "target")
    target.port = 0
    return cfg, target, Path(result["path"]), Path(result["member_key_path"])


def test_round_trip_decrypts_and_keeps_key_separate(snapshot):
    cfg, target, folder, key = snapshot
    assert key.parent == folder.parent / "member-keys"
    assert not list(folder.rglob("*.key"))
    lines = []
    restore(target, folder, apply_changes=True, out=lines.append)
    assert any("member encryption key" in line for line in lines)
    db = Database(target.db_path)
    try:
        assert MemberKeys(target, db).get("u-synthetic", "codex") == CANARY
    finally:
        db.close()
    restored = target.data_dir / member_key.KEY_FILE
    assert restored.read_bytes() == (cfg.data_dir / member_key.KEY_FILE).read_bytes()
    if os.name == "nt":
        script = ("$a=Get-Acl -LiteralPath $env:HARNESS_BACKUP_KEY_PATH; "
                  "$a.AreAccessRulesProtected; $a.Access.Count; "
                  "$a.Access[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value; "
                  "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True,
                                text=True, env={**os.environ, "HARNESS_BACKUP_KEY_PATH": str(restored)})
        rows = result.stdout.splitlines()
        assert rows[:2] == ["True", "1"] and rows[2] == rows[3]
    else:
        assert restored.stat().st_mode & 0o777 == 0o600
        assert key.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["missing", "invalid", "mismatch", "old", "bad_digest"])
def test_unavailable_key_warns_and_restores_database(snapshot, damage):
    _, target, folder, key = snapshot
    if damage == "missing":
        key.unlink()
    elif damage == "invalid":
        key.write_bytes(b"invalid")
    elif damage == "mismatch":
        key.write_bytes(base64.b64encode(b"x" * 32))
    else:
        with sqlite3.connect(folder / "harness.sqlite3") as conn:
            if damage == "old":
                conn.execute("DROP TABLE backup_member_key")
            else:
                conn.execute("UPDATE backup_member_key SET fingerprint='../escape'")
        conn.close()  # checkpoint WAL before restore's immutable read
    lines = []
    restore(target, folder, apply_changes=True, out=lines.append)
    assert target.db_path.is_file()
    assert not (target.data_dir / member_key.KEY_FILE).exists()
    assert any("members must re-add their API keys" in line for line in lines)
    if damage == "mismatch":
        assert any("does not match" in line for line in lines)


def test_custom_directory_and_key_rotation_preserve_old_copy(snapshot, tmp_path):
    cfg, target, folder, old = snapshot
    original = old.read_bytes()
    cfg.backup.member_key_dir = str(tmp_path / "separate")
    db = Database(cfg.db_path)
    try:
        result = asyncio.run(BackupService(cfg, db).backup())
        target.backup.member_key_dir = cfg.backup.member_key_dir
        assert plan(target, folder).items[1].source == Path(result["member_key_path"])
        (cfg.data_dir / member_key.KEY_FILE).write_bytes(base64.b64encode(b"z" * 32))
        rotated = asyncio.run(BackupService(cfg, db).backup())
        assert rotated["member_key_path"] != result["member_key_path"]
        assert Path(result["member_key_path"]).read_bytes() == original
        (cfg.data_dir / member_key.KEY_FILE).unlink()
        asyncio.run(BackupService(cfg, db).backup())
        assert member_key.expected_fingerprint(folder / "harness.sqlite3") == ""
    finally:
        db.close()


@pytest.mark.parametrize("sub", ["", "2026-10-06", "2026-10-06/nested", "2026-10-06.partial"])
def test_rejects_key_directory_in_database_snapshot(tmp_path, sub):
    root = tmp_path / "backups"
    cfg = SimpleNamespace(backup=BackupConfig(dir=str(root), member_key_dir=str(root / sub)))
    with pytest.raises(ValueError, match="separate"):
        member_key.key_dir(cfg, root)
    assert "separate" in settings.check_backup(cfg)[0]


def test_private_write_failure_cleans_up(tmp_path, monkeypatch):
    def fail(path):
        raise OSError("permissions failed")
    monkeypatch.setattr(member_key, "restrict", fail)
    with pytest.raises(OSError, match="permissions failed"):
        member_key.write_private(tmp_path / "key", b"synthetic")
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="32-byte"):
        member_key.fingerprint(base64.b64encode(b"short"))


def test_dry_run_and_apply_preserve_previous_key(snapshot):
    _, target, folder, _ = snapshot
    target.data_dir.mkdir(parents=True)
    existing = target.data_dir / member_key.KEY_FILE
    member_key.write_private(existing, base64.b64encode(b"e" * 32))
    original = existing.read_bytes()
    restore(target, folder, out=lambda *_: None)
    assert existing.read_bytes() == original and not target.db_path.exists()
    previous = restore(target, folder, apply_changes=True, out=lambda *_: None)
    assert (previous / member_key.KEY_FILE).read_bytes() == original
    assert existing.read_bytes() != original


def test_key_changed_after_plan_rolls_back(snapshot):
    _, target, folder, key = snapshot
    target.data_dir.mkdir(parents=True)
    existing = target.data_dir / member_key.KEY_FILE
    member_key.write_private(existing, base64.b64encode(b"e" * 32))
    original = existing.read_bytes()
    p = plan(target, folder)
    key.write_bytes(base64.b64encode(b"x" * 32))
    with pytest.raises(RestoreRefused, match="changed after planning"):
        apply(target, p)
    assert existing.read_bytes() == original and not target.db_path.exists()


def test_doctor_reports_copy_and_warning(snapshot, monkeypatch):
    import httpx
    _, _, _, key = snapshot
    body = {"backup": {"enabled": True, "ok_at": 1, "member_key_path": str(key)}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: SimpleNamespace(json=lambda: body))
    lines = []
    report = SimpleNamespace(ok=lambda *a: lines.append(a), warn=lambda *a: lines.append(a))
    doctor(report, SimpleNamespace(port=0))
    assert member_key.WARNING in lines[-1][1]
    key.unlink()
    doctor(report, SimpleNamespace(port=0))
    assert "No key copy available" in lines[-1][1]
