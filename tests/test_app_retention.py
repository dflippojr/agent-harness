"""#330 stage (b), part 2: an App's session files live in its own folder, an App deletes its sessions, retention
erases them, and revoking an App erases its store and folder after a grace the owner can undo."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from harness import storage
from harness.api import create_app
from harness.app_stores import APP_STORE_FILE, SessionStores
from harness.db import APP_ERASE_GRACE_SECONDS, Database
from harness.llm import Completion
from harness.manager import Manager

from test_app_stores import BALANCE, _fill, _key, _session, rows, rows_of, session_ids
from test_daemon import Script, call, make_cfg
from test_phase6 import wait_for

DAY = 86400


def _files(cfg, s: dict) -> list[Path]:
    """The session's workspace, checkpoint store and transcript paths."""
    dirs = storage.session_dirs(cfg, s)
    return [Path(s["workspace"]), dirs["checkpoints"] / s["id"], dirs["transcripts"] / f"{s['id']}.md"]


def _done(client, sid: str, auth: dict | None = None) -> bool:
    path = f"/api/v1/sessions/{sid}" if auth else f"/sessions/{sid}"
    return client.get(path, headers=auth or {}).json()["status"] == "done"


# files --------------------------------------------------------------------------------------------------------------
def test_new_app_sessions_keep_their_files_in_the_app_folder(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        app_id, auth = _key(client, "shop", "sessions")
        sid = client.post("/api/v1/sessions", headers=auth, json={"prompt": "hello"}).json()["id"]
        tools_only = client.post("/api/v1/sessions", headers=auth, json={
            "prompt": "balance", "tools_only": True, "tools": [BALANCE]}).json()["id"]
        owner_sid = client.post("/sessions", json={"prompt": "owner task", "project": "scratch"}).json()["id"]
        wait_for(lambda: _done(client, sid, auth) and _done(client, owner_sid))
        s, owner = m.db.get_session(sid), m.db.get_session(owner_sid)
        folder = Path(cfg.data_dir) / "apps" / app_id
        assert Path(s["workspace"]) == folder / "workspaces" / sid and Path(s["workspace"]).is_dir()
        assert Path(m.db.get_session(tools_only)["workspace"]).parent == folder / "workspaces"
        wait_for(lambda: (folder / "transcripts" / f"{sid}.md").is_file())
        assert m.runner.checkpointer.store(s).base == folder / "checkpoints" / sid
        assert storage.session_dirs(cfg, s)["artifacts"] == folder / "artifacts"
        # Nothing of the App's session is in the owner's folders; the owner's session stays where it was.
        data = Path(cfg.data_dir)
        assert not any(p.exists() for p in (data / "workspaces" / sid, data / "transcripts" / f"{sid}.md",
                                            data / "checkpoints" / sid))
        assert Path(owner["workspace"]) == data / "workspaces" / owner_sid
        wait_for(lambda: (data / "transcripts" / f"{owner_sid}.md").is_file())


def test_the_file_migration_moves_app_session_files_with_a_backup_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path, apps_dir = data / "harness.sqlite3", data / "apps"
    db = Database(path)
    for sid, app_id in (("own1", ""), ("a1", "k-aaaa"), ("b1", "k-bbbb")):
        db.insert_session(_session(sid, app_id, workspace=str(data / "workspaces" / sid)))
        _fill(db, sid, "ledger")
        (data / "workspaces" / sid).mkdir(parents=True)
        (data / "workspaces" / sid / "notes.txt").write_text(f"work of {sid}")
        (data / "checkpoints" / sid / "repo.git").mkdir(parents=True)
        (data / "transcripts").mkdir(exist_ok=True)
        (data / "transcripts" / f"{sid}.md").write_text(f"transcript of {sid}")
    db.close()

    stores = SessionStores(Database(path), apps_dir)  # moves the rows (stage a), then the files
    for sid, app_id in (("a1", "k-aaaa"), ("b1", "k-bbbb")):
        folder = apps_dir / app_id
        backups = list((folder / "pre-migration").glob("harness-app-files-*.sqlite3"))
        assert len(backups) == 1  # taken before the paths changed
        assert rows(backups[0], "SELECT workspace FROM sessions") == [(str(data / "workspaces" / sid),)]
        assert stores.get_session(sid)["workspace"] == str(folder / "workspaces" / sid)
        assert (folder / "workspaces" / sid / "notes.txt").read_text() == f"work of {sid}"
        assert (folder / "checkpoints" / sid / "repo.git").is_dir()
        assert (folder / "transcripts" / f"{sid}.md").read_text() == f"transcript of {sid}"
        assert not any(p.exists() for p in (data / "workspaces" / sid, data / "checkpoints" / sid,
                                            data / "transcripts" / f"{sid}.md"))
    # The owner's files and path stay.
    assert stores.get_session("own1")["workspace"] == str(data / "workspaces" / "own1")
    assert (data / "workspaces" / "own1" / "notes.txt").is_file() and (data / "checkpoints" / "own1").is_dir()
    stores.close()

    # A second start finds nothing to move: no new backup, nothing changes.
    again = SessionStores(Database(path), apps_dir)
    assert again.migrate_app_files() == []
    assert again.get_session("a1")["workspace"] == str(apps_dir / "k-aaaa" / "workspaces" / "a1")
    again.close()
    assert len(list((apps_dir / "k-aaaa" / "pre-migration").glob("harness-app-files-*.sqlite3"))) == 1


def test_a_file_migration_cut_short_is_finished_on_the_next_start(tmp_path):
    data = tmp_path / "data"
    apps_dir = data / "apps"
    stores = SessionStores(Database(data / "harness.sqlite3"), apps_dir)
    stores.insert_session(_session("a1", "k-aaaa", workspace=str(data / "workspaces" / "a1")))
    stores.close()
    # The files had moved, the stored path not yet.
    (apps_dir / "k-aaaa" / "workspaces" / "a1").mkdir(parents=True)

    again = SessionStores(Database(data / "harness.sqlite3"), apps_dir)
    assert again.get_session("a1")["workspace"] == str(apps_dir / "k-aaaa" / "workspaces" / "a1")
    assert len(list((apps_dir / "k-aaaa" / "pre-migration").glob("*.sqlite3"))) == 1
    again.close()


# delete -------------------------------------------------------------------------------------------------------------
def test_an_app_deletes_its_session_and_everything_tied_to_it(tmp_path):
    def step(messages):  # ask the App for a balance once, then answer
        if any(m["role"] == "tool" for m in messages):
            return Completion(content="Checking has $120.")
        return Completion(tool_calls=[call("get_balance", 0, account="checking")])

    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    m = Manager(cfg, chat=Script([step]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions", "approvals")
        _, b = _key(client, "app-b", "sessions", "sessions:all")
        _, owner = _key(client, "control-center", "admin", kind="owner")
        sid = client.post("/api/v1/sessions", headers=a, json={"prompt": "check the pelican ledger",
                                                               "tools": [BALANCE]}).json()["id"]
        running = client.post("/api/v1/sessions", headers=a, json={"prompt": "check the osprey ledger",
                                                                   "tools": [BALANCE]}).json()["id"]
        kept = client.post("/api/v1/sessions", headers=a, json={"prompt": "check the pelican savings"}).json()["id"]

        def answer() -> bool:
            for c in client.get(f"/api/v1/sessions/{sid}/tool_calls", headers=a).json():
                client.post(f"/api/v1/sessions/{sid}/tool_calls/{c['call_id']}", headers=a, json={"output": "$120"})
            return _done(client, sid, a)
        wait_for(answer, timeout=60)
        wait_for(lambda: _done(client, kept, a))
        wait_for(lambda: client.get(f"/api/v1/sessions/{running}/tool_calls", headers=a).json())
        m.db.put_artifact(sid, "h1", "artifact body")
        m.db.insert_approval({"id": f"ap-{sid}", "session_id": sid, "tool_call_id": "c9", "tool": "write_file",
                              "args": {"path": "x"}, "reason": "ask"})
        s = m.db.get_session(sid)
        transcript = storage.session_dirs(cfg, s)["transcripts"] / f"{sid}.md"
        wait_for(transcript.is_file)
        store = Path(cfg.data_dir) / "apps" / a_id / APP_STORE_FILE
        held = rows_of(store, {sid})
        assert held["events"] and held["app_tool_calls"] and held["search_index"] and held["artifacts"]
        assert Path(s["workspace"]).is_dir()
        token = m.db.get_approval(f"ap-{sid}")["token"]

        # Another App (sessions:all too), the owner (token or Web) and the owner's admin route get a 404.
        assert client.delete(f"/api/v1/sessions/{sid}", headers=b).status_code == 404
        assert client.delete(f"/api/v1/sessions/{sid}", headers=owner).status_code == 404
        assert client.delete(f"/api/v1/sessions/{sid}").status_code == 404
        assert client.delete(f"/api/admin/v1/sessions/{sid}").status_code in (404, 405)
        assert m.db.get_session(sid) is not None

        assert client.delete(f"/api/v1/sessions/{sid}", headers=a).status_code == 204
        assert client.get(f"/api/v1/sessions/{sid}", headers=a).status_code == 404
        assert not any(rows_of(store, {sid}).values())
        assert not any(p.exists() for p in _files(cfg, s))
        assert m.db.get_approval(f"ap-{sid}") is None and m.db.approval_by_token(token) is None
        assert m.db.app_of(sid) == ""
        found = client.get("/api/v1/search?q=pelican", headers=a).json()["results"]
        assert [r["id"] for r in found] == [kept]
        # Idempotent: erasing it again succeeds and changes nothing.
        assert client.delete(f"/api/v1/sessions/{sid}", headers=a).status_code == 204
        assert m.db.get_session(kept) is not None

        # A running session is cancelled, then erased.
        assert m.db.get_session(running)["status"] == "waiting_app"
        assert client.delete(f"/api/v1/sessions/{running}", headers=a).status_code == 204
        assert m.db.get_session(running) is None and running not in m.tasks
        assert not any(rows_of(store, {running}).values())
        # The owner's own sessions can't be deleted through it either.
        owner_sid = client.post("/sessions", json={"prompt": "owner task"}).json()["id"]
        assert client.delete(f"/api/v1/sessions/{owner_sid}", headers=a).status_code == 404
        assert client.delete(f"/api/v1/sessions/{owner_sid}", headers=owner).status_code == 404
        assert m.db.get_session(owner_sid) is not None


# retention ----------------------------------------------------------------------------------------------------------
def _age(m: Manager, sid: str, days: float) -> None:
    """Make session `sid` look idle for `days`."""
    then = time.time() - days * DAY
    store = m.db.for_session(sid)
    store.write(lambda: (store.conn.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (then, sid)),
                         store.conn.execute("UPDATE events SET ts = ? WHERE session_id = ?", (then, sid))))


def test_retention_days_and_the_app_default_expire_sessions_in_the_sweep_while_the_app_is_offline(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions")
        _, b = _key(client, "app-b", "sessions")
        sids = {}
        for name, auth, body in (("own_short", a, {"retention_days": 1}), ("own_long", a, {"retention_days": 30}),
                                 ("default", a, {}), ("none", b, {}), ("b_short", b, {"retention_days": 2})):
            sids[name] = client.post("/api/v1/sessions", headers=auth,
                                     json={"prompt": f"task {name}", **body}).json()["id"]
        assert client.post("/api/v1/sessions", headers=a, json={"prompt": "x", "retention_days": 0}).status_code == 422
        owner_sid = client.post("/sessions", json={"prompt": "owner task"}).json()["id"]
        for sid in (*sids.values(), owner_sid):
            wait_for(lambda sid=sid: m.db.get_session(sid)["status"] == "done")
        assert m.db.get_session(sids["own_short"])["retention_days"] == 1

        # The owner sets App A's default; a bad value or an unknown App is refused.
        assert client.put(f"/api/admin/v1/apps/{a_id}/retention", json={"retention_days": 7}).json()[
            "retention_days"] == 7
        assert client.put(f"/api/admin/v1/apps/{a_id}/retention", json={"retention_days": -1}).status_code == 422
        assert client.put("/api/admin/v1/apps/k-nope/retention", json={"retention_days": 3}).status_code == 404
        assert {k["id"]: k for k in client.get("/keys").json()}[a_id]["retention_days"] == 7
        for sid in (*sids.values(), owner_sid):
            _age(m, sid, 10)
        _age(m, sids["b_short"], 1)  # still inside its 2 days
    files = {name: _files(cfg, m.db.get_session(sid)) for name, sid in sids.items()}

    # The sweep runs with neither App connected: nothing it does needs them.
    report = asyncio.run(m.sweep_app_data())
    assert sorted(report["sessions_expired"]) == sorted([sids["own_short"], sids["default"]])
    assert m.db.get_session(sids["own_short"]) is None and m.db.get_session(sids["default"]) is None
    assert not any(p.exists() for name in ("own_short", "default") for p in files[name])
    for name in ("own_long", "none", "b_short"):
        assert m.db.get_session(sids[name]) is not None, name
    assert m.db.get_session(owner_sid) is not None
    assert asyncio.run(m.sweep_app_data())["sessions_expired"] == []
    m.db.close()


def test_the_maintenance_cleanup_runs_the_app_sweep(tmp_path):
    cfg = make_cfg(tmp_path)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="ok")]))
        await m.start(maintenance=False)
        key, _ = m.db.create_api_key("shop", "sessions", "app")
        m.db.set_app_retention(key["id"], 1)
        m.db.insert_session(_session("a1", key["id"]))
        _age(m, "a1", 3)
        report = await m.maintenance.cleanup()
        gone = m.db.get_session("a1") is None
        await m.stop()
        return report, gone

    report, gone = asyncio.run(body())
    assert report["sessions_expired"] == ["a1"] and gone


# revoke -------------------------------------------------------------------------------------------------------------
def test_revoke_erases_the_apps_store_and_folder_after_the_grace_leaving_a_tombstone(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions")
        b_id, b = _key(client, "app-b", "sessions")
        a_sid = client.post("/api/v1/sessions", headers=a, json={"prompt": "task a"}).json()["id"]
        b_sid = client.post("/api/v1/sessions", headers=b, json={"prompt": "task b"}).json()["id"]
        for sid, auth in ((a_sid, a), (b_sid, b)):
            wait_for(lambda sid=sid, auth=auth: _done(client, sid, auth))
        folder = Path(cfg.data_dir) / "apps" / a_id
        assert (folder / APP_STORE_FILE).is_file() and (folder / "workspaces" / a_sid).is_dir()

        before = time.time()
        assert client.delete(f"/keys/{a_id}").status_code == 204
        assert client.get("/api/v1/sessions", headers=a).status_code == 401  # its token is dead at once
        pending = client.get("/api/admin/v1/apps/erasures").json()
        assert [p["id"] for p in pending] == [a_id]
        assert before + APP_ERASE_GRACE_SECONDS <= pending[0]["erase_after"] <= time.time() + APP_ERASE_GRACE_SECONDS
        assert {k["id"]: k for k in client.get("/keys").json()}[a_id]["erase_after"] == pending[0]["erase_after"]

        # Inside the grace the sweep keeps everything.
        assert asyncio.run(m.sweep_app_data(time.time() + APP_ERASE_GRACE_SECONDS - 60))["apps_erased"] == []
        assert (folder / APP_STORE_FILE).is_file() and session_ids(folder / APP_STORE_FILE) == {a_sid}

        # After it, the store and folder go; only a tombstone stays in the registry.
        report = asyncio.run(m.sweep_app_data(time.time() + APP_ERASE_GRACE_SECONDS + 60))
        assert report["apps_erased"] == [a_id]
        assert not folder.exists()
        assert m.db.app_session_ids(a_id) == [] and m.db.get_session(a_sid) is None
        tomb = m.db.get_api_key(a_id)
        assert tomb["erased_at"] and tomb["revoked_at"] and tomb["erase_after"] is None and tomb["scopes"] == ""
        assert client.get("/api/admin/v1/apps/erasures").json() == []
        assert "store" not in {k["id"]: k for k in client.get("/keys").json()}[a_id]
        assert client.post(f"/api/admin/v1/apps/{a_id}/restore").status_code == 404
        # The other App and the owner are untouched; a later sweep has nothing to do.
        assert client.get(f"/api/v1/sessions/{b_sid}", headers=b).status_code == 200
        assert asyncio.run(m.sweep_app_data(time.time() + 2 * APP_ERASE_GRACE_SECONDS))["apps_erased"] == []
    assert not (Path(cfg.data_dir) / "apps" / a_id).exists()


def test_undo_during_the_grace_keeps_everything_and_issues_a_new_token(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        a_id, a = _key(client, "app-a", "sessions")
        sid = client.post("/api/v1/sessions", headers=a, json={"prompt": "task a"}).json()["id"]
        wait_for(lambda: _done(client, sid, a))
        client.put(f"/api/admin/v1/apps/{a_id}/retention", json={"retention_days": 90})
        assert client.delete(f"/keys/{a_id}").status_code == 204
        assert client.post("/api/admin/v1/apps/k-nope/restore").status_code == 404

        restored = client.post(f"/api/admin/v1/apps/{a_id}/restore")
        assert restored.status_code == 200 and restored.headers["cache-control"] == "no-store"
        new = {"Authorization": f"Bearer {restored.json()['key']}"}
        assert restored.json()["id"] == a_id and not restored.json()["revoked_at"]
        assert client.get("/api/v1/sessions", headers=a).status_code == 401           # the old token stays dead
        assert [s["id"] for s in client.get("/api/v1/sessions", headers=new).json()] == [sid]
        assert client.get(f"/api/v1/sessions/{sid}", headers=new).json()["status"] == "done"
        assert client.get("/api/admin/v1/apps/erasures").json() == []
        assert m.db.get_api_key(a_id)["retention_days"] == 90
        assert asyncio.run(m.sweep_app_data(time.time() + 2 * APP_ERASE_GRACE_SECONDS))["apps_erased"] == []
        assert (Path(cfg.data_dir) / "apps" / a_id / APP_STORE_FILE).is_file()
        assert client.post(f"/api/admin/v1/apps/{a_id}/restore").status_code == 404   # nothing pending any more
        sid2 = client.post("/api/v1/sessions", headers=new, json={"prompt": "again"}).json()["id"]
        wait_for(lambda: _done(client, sid2, new))


def test_owner_keys_have_no_erasure_and_member_storage_and_quota_are_unchanged(tmp_path):
    from harness.storage import account_usage_bytes, ensure_user_dirs

    cfg = make_cfg(tmp_path)
    Path(cfg.data_dir).mkdir(parents=True)
    db = Database(Path(cfg.data_dir) / "harness.sqlite3")
    owner, _ = db.create_api_key("control-center", "admin", "owner")
    assert db.revoke_api_key(owner["id"]) and db.get_api_key(owner["id"])["erase_after"] is None
    assert db.pending_erasures() == []
    db.close()

    data = Path(cfg.data_dir)
    assert storage.workspaces_dir(cfg, "owner") == data / "workspaces"
    assert storage.transcripts_dir(cfg, "owner") == data / "transcripts"
    assert storage.checkpoints_dir(cfg, "owner") == data / "checkpoints"
    assert storage.workspaces_dir(cfg, "u-123") == data / "users" / "u-123" / "workspaces"
    assert storage.checkpoints_dir(cfg, "u-123") == data / "users" / "u-123" / "checkpoints"
    assert storage.workspaces_dir(cfg, "owner", "k-aaaa") == data / "apps" / "k-aaaa" / "workspaces"
    ensure_user_dirs(cfg, "u-123")
    (data / "users" / "u-123" / "workspaces" / "f.txt").write_bytes(b"x" * 1000)
    storage.ensure_app_dirs(cfg, "k-aaaa")
    (data / "apps" / "k-aaaa" / "workspaces" / "big.bin").write_bytes(b"x" * 5000)
    assert account_usage_bytes(cfg, "u-123") == 1000            # an App's files never count toward a member
    (data / "workspaces").mkdir(exist_ok=True)
    (data / "workspaces" / "o.txt").write_bytes(b"x" * 300)
    assert account_usage_bytes(cfg, "owner") == 300             # nor toward the owner
    try:
        storage.workspaces_dir(cfg, "owner", "../escape")
    except storage.ContainmentError:
        pass
    else:
        raise AssertionError("a malformed App id must be refused")


def test_the_cleanup_sweeps_app_workspace_roots(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    orphan = Path(cfg.data_dir) / "apps" / "k-aaaa" / "workspaces" / "gone1"
    orphan.mkdir(parents=True)
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    report: dict = {"orphans_removed": [], "workspaces_removed": [], "kept": []}
    m.maintenance._workspaces(time.time(), report)
    assert report["orphans_removed"] == ["gone1"] and not orphan.exists()
    m.db.close()
