"""Verify and restore of nightly backups (#374), on temporary data dirs only."""

import asyncio
import hashlib
import http.server
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

from harness import backup_restore, storage
from harness.backup_restore import RestoreRefused, restore, verify
from harness.config import BackupConfig
from harness.db import Database
from harness.maintenance import Maintenance
from harness.manager import Manager
from test_app_stores import _session
from test_daemon import Completion, Script, make_cfg


def _cfg(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=False, dir=str(tmp_path / "backups"), keep_days=14)
    cfg.allowed_logins = ["owner-login"]  # a member account needs an owner allowlist
    return cfg


def _hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _quiet(*_args) -> None:
    pass


async def _seed_and_back_up(cfg) -> tuple[Path, dict]:
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    await m.start(maintenance=False)
    first, _ = m.db.create_api_key("first", scopes="sessions", kind="app")
    second, _ = m.db.create_api_key("second", scopes="sessions", kind="app")
    gone, _ = m.db.create_api_key("gone", scopes="sessions", kind="app")
    apps = {"first": first["id"], "second": second["id"], "gone": gone["id"]}
    m.db.insert_account({"user_id": "u-member", "role": "member", "login": "member", "display_name": "member",
                         "enabled": 1, "disk_quota_bytes": 0, "max_running": 1, "max_queued": 1,
                         "created_at": 1, "updated_at": 1, "last_activity_at": 0})
    for sid, app_id in (("own1", ""), ("a1", apps["first"]), ("a2", apps["first"]), ("b1", apps["second"]),
                        ("g1", apps["gone"])):
        m.db.insert_session(_session(sid, app_id))
        m.db.insert_event(sid, "user_message", {"content": f"hello {sid}"})
    _write(storage.transcripts_dir(cfg, "owner") / "own1.md", "owner transcript")
    _write(storage.transcripts_dir(cfg, "u-member") / "m1.md", "member transcript")
    for name in ("first", "gone"):
        _write(storage.transcripts_dir(cfg, "owner", apps[name]) / "s.md", f"{name} transcript")
    # Revoked and erased after the store was written: the backup still holds its store and transcripts.
    m.db.revoke_api_key(apps["gone"])
    m.db.mark_app_erased(apps["gone"])
    result = await m.maintenance.backup()
    await m.stop()
    m.db.close()
    return Path(result["path"]), apps


def test_round_trip_restores_every_store_and_transcript_to_a_working_daemon(tmp_path):
    cfg = _cfg(tmp_path)
    folder, apps = asyncio.run(_seed_and_back_up(cfg))
    backup = _hashes(folder)
    assert verify(folder) == []
    data = Path(cfg.data_dir)

    # Damage the live files: a garbage main store, a lost App store, rewritten transcripts.
    cfg.db_path.write_bytes(b"not a database")
    first_store = data / "apps" / apps["first"] / "harness.sqlite3"
    first_store.unlink()
    _write(storage.transcripts_dir(cfg, "owner") / "own1.md", "damaged")
    _write(storage.transcripts_dir(cfg, "u-member") / "m1.md", "damaged")
    gone_transcript = storage.transcripts_dir(cfg, "owner", apps["gone"]) / "s.md"
    gone_transcript.unlink()

    lines: list[str] = []
    previous = restore(cfg, folder, apply_changes=True, out=lines.append)
    assert previous is not None
    assert previous.parent == data and previous.name.startswith("restore-") and previous.name.endswith("-previous")
    assert (previous / "harness.sqlite3").read_bytes() == b"not a database"
    assert (previous / "transcripts" / "own1.md").read_text() == "damaged"
    assert (previous / "users" / "u-member" / "transcripts" / "m1.md").read_text() == "damaged"
    assert "harness.sqlite3" in (previous / backup_restore.MANIFEST).read_text()
    assert any("To undo" in line for line in lines)
    gone = apps["gone"]
    assert any(line.startswith("WARN") and f"App store {gone}" in line for line in lines)
    assert any(line.startswith("WARN") and f"App {gone}'s transcripts" in line for line in lines)
    assert not gone_transcript.exists()  # the erased App's archive was skipped

    assert storage.transcripts_dir(cfg, "owner").joinpath("own1.md").read_text() == "owner transcript"
    assert storage.transcripts_dir(cfg, "u-member").joinpath("m1.md").read_text() == "member transcript"
    first_transcript = storage.transcripts_dir(cfg, "owner", apps["first"]) / "s.md"
    assert first_transcript.read_text() == "first transcript"

    async def fresh():
        m = Manager(cfg, chat=Script([Completion(content="ok")]))
        await m.start(maintenance=False)
        try:
            return {sid: (m.db.get_session(sid) or {}).get("app_id")
                    for sid in ("own1", "a1", "a2", "b1")}, [e["data"] for e in m.db.events("a1")]
        finally:
            await m.stop()
            m.db.close()

    sessions, events = asyncio.run(fresh())
    assert sessions == {"own1": "", "a1": apps["first"], "a2": apps["first"], "b1": apps["second"]}
    assert {"content": "hello a1"} in events
    assert _hashes(folder) == backup  # verify and restore never write into the backup


def test_restore_without_apply_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    folder, _ = asyncio.run(_seed_and_back_up(cfg))
    _write(storage.transcripts_dir(cfg, "owner") / "own1.md", "newer")
    before = _hashes(Path(cfg.data_dir))
    lines: list[str] = []
    assert restore(cfg, folder, out=lines.append) is None
    assert _hashes(Path(cfg.data_dir)) == before
    assert not list(Path(cfg.data_dir).glob("restore-*"))
    assert any(line.startswith("replace file") and "main store" in line for line in lines)
    assert lines[-1].startswith("Dry run")


def test_include_config_restores_config_and_overlay(tmp_path):
    cfg = _cfg(tmp_path)
    folder, _ = asyncio.run(_seed_and_back_up(cfg))
    _write(folder / "config" / "harness.yaml", "port: 1\n")
    _write(folder / "managed-config.json", "{}\n")
    config_dir = tmp_path / "config"
    _write(config_dir / "harness.yaml", "port: 2\n")

    lines: list[str] = []
    restore(cfg, folder, apply_changes=True, out=lines.append)
    assert (config_dir / "harness.yaml").read_text() == "port: 2\n"  # not without --include-config
    assert any("--include-config" in line for line in lines)

    previous = restore(cfg, folder, apply_changes=True, include_config=True, config_dir=config_dir, out=_quiet)
    assert previous is not None
    assert (config_dir / "harness.yaml").read_text() == "port: 1\n"
    assert (Path(cfg.data_dir) / "managed-config.json").read_text() == "{}\n"
    assert (previous / "config" / "harness.yaml").read_text() == "port: 2\n"


# refusals
@pytest.fixture
def backed_up(tmp_path):
    """A small backup built by the real Maintenance, without a Manager."""
    cfg = _cfg(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    db = Database(cfg.db_path)
    db.insert_session(_session("s1"))
    _write(storage.transcripts_dir(cfg, "owner") / "nested" / "s1.md", "x" * 4000)
    result = asyncio.run(Maintenance(cfg, db, None).backup())
    db.close()
    return cfg, Path(result["path"])


def _corrupt_sqlite(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    raw[:16] = b"garbage garbage!"
    path.write_bytes(bytes(raw))


def _too_new(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99999")
    conn.close()


def _bad_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()[0]
    raw = bytearray(path.read_bytes())
    offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + 5
    raw[offset] ^= 0xFF  # inside the compressed data: the CRC check catches it
    path.write_bytes(bytes(raw))


@pytest.mark.parametrize("damage, target, expected", [
    (_corrupt_sqlite, "harness.sqlite3", "harness.sqlite3"),
    (_too_new, "harness.sqlite3", "schema v99999"),
    (_bad_zip, "transcripts.zip", "transcripts.zip"),
])
def test_verify_fails_and_restore_refuses_a_damaged_backup(backed_up, damage, target, expected):
    cfg, folder = backed_up
    assert verify(folder) == []
    damage(folder / target)
    problems = verify(folder)
    assert problems and expected in problems[0]
    assert backup_restore.main(["verify", str(folder)]) == 1
    before = _hashes(Path(cfg.data_dir))
    with pytest.raises(RestoreRefused, match="failed verification"):
        restore(cfg, folder, apply_changes=True, out=_quiet)
    assert _hashes(Path(cfg.data_dir)) == before


def test_verify_fails_for_badly_named_files_and_unsafe_zip_paths(backed_up):
    _, folder = backed_up
    (folder / "apps").mkdir(exist_ok=True)
    (folder / "apps" / "not an app.sqlite3").write_bytes(b"")
    (folder / "transcripts" / "users").mkdir(parents=True)
    with zipfile.ZipFile(folder / "transcripts" / "users" / "u-x.zip", "w") as archive:
        archive.writestr("../escape.md", "x")
    problems = verify(folder)
    assert any("not an app.sqlite3" in p for p in problems)
    assert any("unsafe path" in p for p in problems)


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):  # noqa: A002 - the base class's parameter name
        pass


def test_restore_refuses_while_the_daemon_answers(backed_up):
    cfg, folder = backed_up
    server = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg.port = server.server_address[1]
    try:
        before = _hashes(Path(cfg.data_dir))
        with pytest.raises(RestoreRefused, match="answers on port"):
            restore(cfg, folder, apply_changes=True, out=_quiet)
        assert _hashes(Path(cfg.data_dir)) == before
    finally:
        server.shutdown()
        server.server_close()


def test_restore_refuses_while_a_store_is_write_locked(backed_up):
    cfg, folder = backed_up
    holder = sqlite3.connect(str(cfg.db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RestoreRefused, match="locked"):
            restore(cfg, folder, apply_changes=True, out=_quiet)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_failed_copy_puts_the_moved_files_back(backed_up, monkeypatch):
    cfg, folder = backed_up
    before = _hashes(Path(cfg.data_dir))

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_restore.shutil, "copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        restore(cfg, folder, apply_changes=True, out=_quiet)
    after = {k: v for k, v in _hashes(Path(cfg.data_dir)).items() if not k.startswith("restore-")}
    assert after == before


def test_the_cli_verifies_and_dry_runs_against_the_configured_data_dir(backed_up, monkeypatch, capsys):
    from harness import config as config_mod

    cfg, folder = backed_up
    seen = []
    monkeypatch.setattr(config_mod, "load", lambda config_dir=None: seen.append(config_dir) or cfg)
    assert backup_restore.main(["verify", str(folder)]) == 0
    assert capsys.readouterr().out.strip().endswith("OK")
    before = _hashes(Path(cfg.data_dir))
    assert backup_restore.main(["--config-dir", "cfgdir", "restore", str(folder)]) == 0
    assert seen == ["cfgdir"]
    assert "Dry run" in capsys.readouterr().out
    assert _hashes(Path(cfg.data_dir)) == before
    _too_new(folder / "harness.sqlite3")
    assert backup_restore.main(["restore", str(folder), "--apply"]) == 1
    assert "Refused, nothing changed" in capsys.readouterr().err
    assert _hashes(Path(cfg.data_dir)) == before
