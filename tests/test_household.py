"""Issue #62: owner-provisioned household accounts with strict isolation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.access import resolve_access
from harness.accounts import AccountService, DEFAULT_DISK_QUOTA_BYTES
from harness.admin import API_VERSION as ADMIN_API_VERSION, PREFIX
from harness.api import create_app
from harness.apps import API_VERSION as APP_API_VERSION
from harness.clone import CloneRefused, QuotaExceeded, isolated_clone_env, isolated_prepare, isolated_refresh_origin, public_https_url
from harness.config import GuestAccess, Project
from harness.db import Database
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.principal import OWNER_USER_ID, open_owner_mode, require_owner_allowlist, resolve_human
from harness.scheduler import GpuScheduler
from harness.storage import (ContainmentError, account_usage_bytes, contained, ensure_user_dirs, repos_dir,
                             require_contained, user_root, workspaces_dir)
from harness.tools import ToolError, Workspace

from test_daemon import Script, make_cfg

OWNER = "me@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"
GUEST = "guest@example.com"


def household(tmp_path, steps=None, guests=None) -> tuple[TestClient, Manager]:
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [OWNER]
    cfg.guests = guests or []
    cfg.projects["lab"] = Project(name="lab", homelab=True, memory_library=True, images=True)
    cfg.search.enabled = True
    m = Manager(cfg, chat=Script(steps or [Completion(content="done")]))
    return TestClient(create_app(m)), m


def H(login: str) -> dict:
    return {"Tailscale-User-Login": login}


def create_member(client, login, name, **extra) -> dict:
    body = {"login": login, "display_name": name, **extra}
    r = client.post(f"{PREFIX}/accounts", json=body, headers=H(OWNER))
    assert r.status_code == 201, r.text
    return r.json()


def test_open_owner_legacy_only_without_members(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.allowed_logins == []
    db = Database(cfg.db_path)
    assert open_owner_mode(cfg, db.member_count())
    ident = resolve_human(cfg, "anyone@example.com", db)
    assert ident.kind == "owner" and ident.allowed
    db.close()

    cfg.allowed_logins = [OWNER]
    db = Database(cfg.db_path)
    m = Manager(cfg, db=db, chat=Script([Completion(content="x")]))
    AccountService(m).create(OWNER_USER_ID, ALICE, "Alice")
    db.close()

    cfg_open = make_cfg(tmp_path)
    cfg_open.data_dir = cfg.data_dir
    cfg_open.allowed_logins = []
    with pytest.raises(ValueError, match="explicit allowed_logins"):
        require_owner_allowlist(cfg_open, 1)
    with pytest.raises(ValueError, match="explicit allowed_logins"):
        Manager(cfg_open, db=Database(cfg.db_path))


def test_first_member_requires_owner_allowlist(tmp_path):
    cfg = make_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="x")]))
    with pytest.raises(HarnessError) as exc:
        AccountService(m).create(OWNER_USER_ID, ALICE, "Alice")
    assert exc.value.status == 400
    assert "allowlist" in str(exc.value)


def test_migration_keeps_owner_paths_and_user_id(tmp_path):
    client, m = household(tmp_path)
    with client:
        s = client.post("/sessions", json={"prompt": "hello"}, headers=H(OWNER)).json()
        row = m.db.get_session(s["id"])
        assert row["owner_id"] == OWNER_USER_ID
        ws = Path(row["workspace"])
        assert ws.parent == m.cfg.workspaces_dir
        assert "users" not in ws.parts
        assert client.get("/projects", headers=H(OWNER)).json()
        names = [p["name"] for p in client.get("/projects", headers=H(OWNER)).json()]
        assert "scratch" in names and "lab" in names


def test_identity_precedence_and_single_role(tmp_path):
    guests = [GuestAccess(login=GUEST, until="2099-01-01T00:00:00+00:00")]
    client, m = household(tmp_path, guests=guests)
    with client:
        alice = create_member(client, ALICE, "Alice")
        assert alice["user_id"].startswith("u-") and alice["user_id"] != OWNER_USER_ID
        assert alice["login"] == ALICE and alice["enabled"] is True
        assert alice["disk_quota_bytes"] == DEFAULT_DISK_QUOTA_BYTES
        assert alice["max_running"] == 1 and alice["max_queued"] == 2
        assert "prompt" not in alice and "repo" not in str(alice)

        assert client.post(f"{PREFIX}/accounts", json={"login": OWNER, "display_name": "Nope"},
                           headers=H(OWNER)).status_code == 400
        assert client.post(f"{PREFIX}/accounts", json={"login": GUEST, "display_name": "Nope"},
                           headers=H(OWNER)).status_code == 400
        assert client.post(f"{PREFIX}/accounts", json={"login": ALICE, "display_name": "Dup"},
                           headers=H(OWNER)).status_code == 409

        owner = resolve_access(m.cfg, OWNER, m.db)
        member = resolve_access(m.cfg, ALICE, m.db)
        guest = resolve_access(m.cfg, GUEST, m.db)
        unknown = resolve_access(m.cfg, "stranger@example.com", m.db)
        local = resolve_access(m.cfg, None, m.db)
        assert owner.kind == "owner" and owner.user_id == OWNER_USER_ID
        assert member.kind == "member" and member.user_id == alice["user_id"] and member.bundled
        assert guest.kind == "guest" and guest.allowed
        assert not unknown.allowed
        assert local.kind == "owner"

        renamed = client.patch(f"{PREFIX}/accounts/{alice['user_id']}",
                               json={"display_name": "Alicia"}, headers=H(OWNER)).json()
        assert renamed["display_name"] == "Alicia" and renamed["user_id"] == alice["user_id"]
        rebound = client.patch(f"{PREFIX}/accounts/{alice['user_id']}",
                               json={"login": "alice2@example.com"}, headers=H(OWNER)).json()
        assert rebound["login"] == "alice2@example.com" and rebound["user_id"] == alice["user_id"]
        assert m.stream_epoch.get(alice["user_id"], 0) >= 1
        old = client.get("/me", headers=H(ALICE))
        assert old.status_code == 403
        me = client.get("/me", headers=H("alice2@example.com")).json()
        assert me["role"] == "member" and me["user_id"] == alice["user_id"]


def test_discovery_hides_project_names(tmp_path):
    client, _ = household(tmp_path)
    with client:
        root = client.get("/api/v1").json()
        assert root["projects"] == []
        assert root["api_version"] == APP_API_VERSION
        assert root["features"]["scoped_projects"] is True
        assert "scratch" not in str(root["projects"])
        listed = client.get("/api/v1/projects", headers=H(OWNER)).json()
        assert any(p["name"] == "scratch" for p in listed)


def test_two_member_adversarial_matrix(tmp_path):
    client, m = household(tmp_path, steps=[Completion(content="secret-owner"),
                                           Completion(content="secret-alice"),
                                           Completion(content="secret-bob")])
    with client:
        alice = create_member(client, ALICE, "Alice")
        bob = create_member(client, BOB, "Bob")
        ah, bh = H(ALICE), H(BOB)
        owner_s = client.post("/sessions", json={"prompt": "owner unique zebra prompt"},
                              headers=H(OWNER)).json()
        alice_s = client.post("/api/v1/sessions", json={"prompt": "alice unique mango prompt"},
                              headers=ah).json()
        bob_s = client.post("/api/v1/sessions", json={"prompt": "bob unique papaya prompt"},
                            headers=bh).json()
        assert owner_s["id"] != alice_s["id"] != bob_s["id"]
        assert m.db.get_session(alice_s["id"])["owner_id"] == alice["user_id"]
        assert Path(m.db.get_session(alice_s["id"])["workspace"]).is_relative_to(
            user_root(m.cfg, alice["user_id"]))

        def same_404(resp):
            assert resp.status_code == 404
            assert resp.json()["detail"] == "no session matches that id"

        same_404(client.get(f"/api/v1/sessions/{owner_s['id']}", headers=ah))
        same_404(client.get(f"/api/v1/sessions/{bob_s['id']}", headers=ah))
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}", headers=bh))
        same_404(client.get(f"/sessions/{alice_s['id']}", headers=H(OWNER)))
        same_404(client.get("/api/v1/sessions/nosuchidxx", headers=ah))
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}/transcript", headers=bh))
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}/changes", headers=bh))
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}/approvals", headers=bh))
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}/events", params={"follow": False}, headers=bh))
        same_404(client.get(f"/api/v1/sessions/{owner_s['id']}/transcript", headers=ah))
        same_404(client.get(f"/api/v1/sessions/{bob_s['id']}/events", params={"follow": False}, headers=ah))
        assert client.post(f"/api/v1/sessions/{alice_s['id']}/cancel", headers=bh).status_code == 404
        assert client.post(f"/api/v1/sessions/{alice_s['id']}/review/merge", headers=bh).status_code == 404
        alice_events = client.get(f"/api/v1/sessions/{alice_s['id']}/events",
                                  params={"follow": False}, headers=ah)
        assert alice_events.status_code == 200
        assert "mango" in alice_events.text and "papaya" not in alice_events.text
        alice_t = client.get(f"/api/v1/sessions/{alice_s['id']}/transcript", headers=ah)
        assert alice_t.status_code == 200 and "mango" in alice_t.text and "papaya" not in alice_t.text

        alice_list = client.get("/api/v1/sessions", headers=ah).json()
        assert [s["id"] for s in alice_list] == [alice_s["id"]]
        owner_list = client.get("/sessions", headers=H(OWNER)).json()
        assert all(s["id"] != alice_s["id"] and s["id"] != bob_s["id"] for s in owner_list)
        assert "mango" not in str(owner_list) and "papaya" not in str(owner_list)

        q = client.get("/api/v1/search", params={"q": "mango"}, headers=ah).json()
        assert q["results"] and all(r["id"] == alice_s["id"] for r in q["results"])
        assert client.get("/api/v1/search", params={"q": "papaya"}, headers=ah).json()["results"] == []
        owner_search = client.get("/search", params={"q": "mango"}, headers=H(OWNER)).json()
        assert owner_search["results"] == []

        alice_projects = client.get("/api/v1/projects", headers=ah).json()
        assert {p["name"] for p in alice_projects} == {"scratch"}
        created = client.post("/api/v1/projects", json={"name": "notes"}, headers=ah).json()
        assert created["name"] == "notes" and created["target"] == "tower"
        assert client.post("/api/v1/projects", json={"name": "notes"}, headers=bh).json()["name"] == "notes"
        owner_names = {p["name"] for p in client.get("/projects", headers=H(OWNER)).json()}
        assert "notes" not in owner_names
        assert "lab" not in {p["name"] for p in client.get("/api/v1/projects", headers=ah).json()}

        queue = client.get("/api/v1/queue", headers=ah).json()
        assert all(item["session_id"] == alice_s["id"] for item in queue)

        me = client.get("/api/v1/me", headers=ah).json()
        assert me["role"] == "member" and me["user_id"] == alice["user_id"]
        assert me["capabilities"]["admin"] is False
        assert "secret" not in str(me)

        admin = client.get(PREFIX, headers=ah)
        assert admin.status_code == 403
        assert "members cannot use the owner API" in admin.json()["detail"]
        assert "accounts" not in admin.json().get("detail", "")
        for path in (f"{PREFIX}/accounts", f"{PREFIX}/sessions", f"{PREFIX}/keys", f"{PREFIX}/gpu",
                     f"{PREFIX}/jobs", f"{PREFIX}/maintenance", "/keys", "/jobs", "/images", "/gpu",
                     "/memory", "/templates", "/metrics", "/runners"):
            r = client.get(path, headers=ah)
            assert r.status_code == 403, path
        assert "runners" in client.get("/runners", headers=ah).json()["detail"]

        app = client.post("/keys", json={"name": "shop", "kind": "app",
                                         "scopes": ["sessions", "sessions:all", "approvals"]},
                          headers=H(OWNER)).json()
        device = client.post("/keys", json={"name": "zed"}, headers=H(OWNER)).json()
        bearer = {"Authorization": f"Bearer {app['key']}"}
        listed = client.get("/api/v1/sessions", headers=bearer).json()
        ids = {s["id"] for s in listed}
        assert alice_s["id"] not in ids and bob_s["id"] not in ids
        same_404(client.get(f"/api/v1/sessions/{alice_s['id']}", headers=bearer))
        missing = client.get("/api/v1/sessions/nosuchidxx", headers=bearer)
        same_404(missing)
        assert client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {device['key']}"}).status_code == 403
        created_app = client.post("/api/v1/sessions", headers=bearer, json={"prompt": "app work"})
        assert created_app.status_code == 201
        assert m.db.get_session(created_app.json()["id"])["owner_id"] == OWNER_USER_ID
        assert client.post("/api/v1/projects", headers=bearer, json={"name": "from-app"}).status_code == 403

        extra = [client.post("/api/v1/sessions", json={"prompt": f"alice extra {i}"}, headers=ah).json()
                 for i in range(3)]
        page = client.get("/api/v1/sessions", params={"limit": 2}, headers=ah).json()
        assert len(page) == 2
        assert all(s["id"] != bob_s["id"] and s["id"] != owner_s["id"] for s in page)
        from harness.search import SessionSearch
        found = SessionSearch(m.db).session_search("zebra", _session=extra[0]["id"])
        assert "No earlier sessions match" in found
        assert owner_s["id"] not in found and bob_s["id"] not in found
        own = SessionSearch(m.db).session_search("mango", _session=extra[0]["id"])
        assert alice_s["id"] in own and bob_s["id"] not in own and owner_s["id"] not in own


def test_member_cannot_use_hosted_or_owner_modules(tmp_path):
    client, m = household(tmp_path)
    with client:
        create_member(client, ALICE, "Alice")
        ah = H(ALICE)
        backends = client.get("/api/v1/backends", headers=ah).json()
        assert [b["name"] for b in backends] == ["local"]
        assert client.post("/api/v1/sessions", json={"prompt": "x", "backend": "claude"},
                           headers=ah).status_code == 403
        assert client.post("/api/v1/images", json={"prompt": "cat"}, headers=ah).status_code == 403
        assert client.get("/api/v1/remote-control", headers=ah).status_code == 403
        s = client.post("/api/v1/sessions", json={"prompt": "list files"}, headers=ah).json()
        row = m.db.get_session(s["id"])
        schemas = m.runner.tool_schemas(row, m.runner.workspace(row))
        names = {t["function"]["name"] for t in schemas}
        for banned in ("homelab_services", "restart_service", "memory_search", "memory_read", "memory_write",
                       "generate_image", "remote_control_status"):
            assert banned not in names, banned
        assert "run_shell" in names and "session_search" in names


def test_member_clone_allows_only_public_https(tmp_path):
    refused = [
        "C:/secret", r"\\server\share", "file:///tmp/repo", "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git", "local:demo", "http://github.com/o/r",
        "https://evil.example/o/r", "https://github.com/o/r.git:2222",
        "https://user:pass@github.com/o/r", "https://github.com/o/r?token=1",
        "https://github.com/o/r#frag", ".", "../etc", "https://github.com/",
    ]
    # urlsplit('https://github.com/o/r:2222') treats :2222 as path, not port — still unapproved if weird.
    for url in refused:
        with pytest.raises(CloneRefused):
            public_https_url(url)
    assert public_https_url("https://github.com/org/repo") == "https://github.com/org/repo"
    assert public_https_url("https://www.gitlab.com/org/repo.git") == "https://gitlab.com/org/repo.git"
    env = isolated_clone_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["credential.helper"] if False else env["GIT_CONFIG_VALUE_0"] == ""

    client, m = household(tmp_path)
    with client:
        create_member(client, ALICE, "Alice")
        ah = H(ALICE)
        for url in ("local:demo", "file:///tmp/x", "D:/Projects/x", "https://example.com/a/b"):
            r = client.post("/api/v1/projects", json={"name": "p", "repo": url}, headers=ah)
            assert r.status_code == 400, url

        def fake_clone(url, dest, root, max_bytes=None):
            dest.mkdir(parents=True)
            (dest / "README.md").write_text("public\n", encoding="utf-8")
            from harness.projects import GitResult
            return GitResult(0, "", "")

        import harness.clone as clone_mod
        orig = clone_mod.clone_public
        clone_mod.clone_public = fake_clone
        try:
            created = client.post("/api/v1/projects", json={
                "name": "pub", "repo": "https://github.com/org/repo",
            }, headers=ah)
            assert created.status_code == 201, created.text
            dest = Path(m.db.get_member_project(
                client.get("/api/v1/me", headers=ah).json()["user_id"], "pub")["repo"])
            assert dest.is_relative_to(user_root(m.cfg, client.get("/api/v1/me", headers=ah).json()["user_id"]))
            assert dest.name == "pub"
        finally:
            clone_mod.clone_public = orig


def test_run_clone_stops_when_dest_exceeds_max_bytes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "blob.bin").write_bytes(b"x" * 80_000)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"],
                   check=True)
    dest = tmp_path / "dest"
    from harness.clone import _run_clone
    with pytest.raises(QuotaExceeded):
        _run_clone(["git", "clone", "--", str(src), str(dest)], dest, max_bytes=2_000)
    assert not dest.exists()


def test_isolated_prepare_stops_when_clone_exceeds_max_bytes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "blob.bin").write_bytes(b"x" * 80_000)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"],
                   check=True)
    dest = tmp_path / "ws" / "sessq01"
    with pytest.raises(QuotaExceeded):
        isolated_prepare(dest, src, "sessq01", tmp_path / "ws", max_bytes=2_000)
    assert not dest.exists()


def test_project_clone_is_removed_when_it_exceeds_quota(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", disk_quota_bytes=4096)
        ah = H(ALICE)

        def fat_clone(url, dest, root, max_bytes=None):
            dest.mkdir(parents=True)
            (dest / "blob.bin").write_bytes(b"x" * 20_000)
            from harness.clone import QuotaExceeded as QE
            raise QE(max_bytes or 0)

        import harness.clone as clone_mod
        orig = clone_mod.clone_public
        clone_mod.clone_public = fat_clone
        try:
            r = client.post("/api/v1/projects", json={
                "name": "linux", "repo": "https://github.com/org/repo",
            }, headers=ah)
            assert r.status_code == 507, r.text
            assert m.db.get_member_project(alice["user_id"], "linux") is None
            dest = repos_dir(m.cfg, alice["user_id"]) / "linux"
            assert not dest.exists()
        finally:
            clone_mod.clone_public = orig


def test_filesystem_isolation_duplicate_slugs_and_containment(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice")
        bob = create_member(client, BOB, "Bob")
        client.post("/api/v1/projects", json={"name": "notes"}, headers=H(ALICE))
        client.post("/api/v1/projects", json={"name": "notes"}, headers=H(BOB))
        assert m.db.get_member_project(alice["user_id"], "notes")
        assert m.db.get_member_project(bob["user_id"], "notes")
        ar = user_root(m.cfg, alice["user_id"])
        br = user_root(m.cfg, bob["user_id"])
        assert ar != br
        ensure_user_dirs(m.cfg, alice["user_id"])
        inside = ar / "repos" / "notes"
        inside.mkdir(parents=True, exist_ok=True)
        require_contained(inside, ar)
        with pytest.raises(ContainmentError):
            require_contained(br, ar)
        with pytest.raises(ContainmentError):
            require_contained(m.cfg.data_dir / "workspaces", ar)
        escaped = ar / ".." / ".." / "etc"
        assert not contained(escaped, ar)
        with pytest.raises(ContainmentError):
            require_contained(Path("/tmp"), ar)

        with pytest.raises(ContainmentError):
            user_root(m.cfg, "../owner")
        with pytest.raises(ContainmentError):
            user_root(m.cfg, "u-abc/../u-def")
        with pytest.raises(ContainmentError):
            user_root(m.cfg, "u-abc\\u-def")
        cafe_nfc = ar / unicodedata.normalize("NFC", "café")
        cafe_nfc.mkdir()
        assert contained(cafe_nfc, ar)
        cafe_nfd = ar / unicodedata.normalize("NFD", "café")
        assert contained(cafe_nfd, ar) or not cafe_nfd.exists()
        escaped_case = Path(str(ar).swapcase()) if os.name == "nt" else ar.parent / (ar.name.upper() + "-x")
        if escaped_case != ar.resolve() and escaped_case.exists():
            with pytest.raises(ContainmentError):
                require_contained(escaped_case, ar)
        assert not contained(ar / ".." / ".." / "scratch", ar)

        link = ar / "escape"
        try:
            link.symlink_to(m.cfg.data_dir)
            assert not contained(link, ar)
            with pytest.raises(ContainmentError):
                require_contained(link, ar)
        except OSError:
            pass

        if os.name == "nt":
            junction = ar / "junc"
            try:
                subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(m.cfg.data_dir)],
                               check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                junction = None
            if junction is not None:
                with pytest.raises(ContainmentError):
                    require_contained(junction, ar)


def test_quota_and_concurrency_and_disable(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", disk_quota_bytes=64, max_running=1, max_queued=1)
        ah = H(ALICE)
        root = ensure_user_dirs(m.cfg, alice["user_id"])
        fat = root / "artifacts" / "blob.bin"
        fat.parent.mkdir(parents=True, exist_ok=True)
        fat.write_bytes(b"x" * 128)
        assert account_usage_bytes(m.cfg, alice["user_id"]) >= 64
        refused = client.post("/api/v1/sessions", json={"prompt": "too big"}, headers=ah)
        assert refused.status_code == 507

        client.patch(f"{PREFIX}/accounts/{alice['user_id']}",
                     json={"disk_quota_bytes": 50 * 2**20, "max_queued": 2}, headers=H(OWNER))
        fat.unlink()
        now = 1_700_000_000.0
        for i in range(2):
            sid = f"q{i}queuedxx"
            ws = m.cfg.data_dir / "users" / alice["user_id"] / "workspaces" / sid
            ws.mkdir(parents=True, exist_ok=True)
            m.db.insert_session({
                "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
                "title": sid, "status": "queued", "workspace": str(ws), "created_at": now, "updated_at": now,
                "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": alice["user_id"],
            })
        third = client.post("/api/v1/sessions", json={"prompt": "three"}, headers=ah)
        assert third.status_code == 429

        running = "runalice01"
        rws = m.cfg.data_dir / "users" / alice["user_id"] / "workspaces" / running
        rws.mkdir(parents=True, exist_ok=True)
        m.db.insert_session({
            "id": running, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
            "title": running, "status": "running", "workspace": str(rws), "created_at": now, "updated_at": now,
            "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": alice["user_id"],
        })
        disabled = client.patch(f"{PREFIX}/accounts/{alice['user_id']}",
                                json={"enabled": False}, headers=H(OWNER)).json()
        assert disabled["enabled"] is False
        denied = client.get("/api/v1/me", headers=ah)
        assert denied.status_code == 403
        assert m.db.get_session(running)["status"] == "cancelled"
        assert rws.exists()
        audit = client.get(f"{PREFIX}/accounts/audit", headers=H(OWNER)).json()
        actions = {a["action"] for a in audit}
        assert "disable" in actions and "create" in actions
        assert all("prompt" not in (a.get("detail") or "") for a in audit)


def test_member_followup_send_respects_queue_and_quota(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", disk_quota_bytes=100_000, max_queued=1)
        uid = alice["user_id"]
        ah = H(ALICE)
        now = 1_700_000_000.0

        def insert(sid, status):
            ws = workspaces_dir(m.cfg, uid) / sid
            ws.mkdir(parents=True, exist_ok=True)
            m.db.insert_session({
                "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
                "title": sid, "status": status, "workspace": str(ws), "created_at": now, "updated_at": now,
                "context": [{"role": "user", "content": "hi"}], "run": {}, "totals": {}, "inbox": [],
                "owner_id": uid,
            })

        insert("donealice01", "done")
        insert("qalice00001", "queued")
        blocked = client.post("/api/v1/sessions/donealice01/messages", json={"content": "again"}, headers=ah)
        assert blocked.status_code == 429, blocked.text
        assert m.db.get_session("donealice01")["status"] == "done"

        m.db.update_session("qalice00001", status="done")
        fat = ensure_user_dirs(m.cfg, uid) / "artifacts" / "blob.bin"
        fat.parent.mkdir(parents=True, exist_ok=True)
        fat.write_bytes(b"x" * 120_000)
        over = client.post("/api/v1/sessions/donealice01/messages", json={"content": "again"}, headers=ah)
        assert over.status_code == 507, over.text
        assert m.db.get_session("donealice01")["status"] == "done"


def test_max_queued_zero_is_rejected(tmp_path):
    client, m = household(tmp_path)
    with client:
        refused = client.post(f"{PREFIX}/accounts", json={
            "login": ALICE, "display_name": "Alice", "max_queued": 0,
        }, headers=H(OWNER))
        assert refused.status_code == 400, refused.text
        alice = create_member(client, ALICE, "Alice")
        patch = client.patch(f"{PREFIX}/accounts/{alice['user_id']}",
                             json={"max_queued": 0}, headers=H(OWNER))
        assert patch.status_code == 400, patch.text
        assert m.db.account_by_id(alice["user_id"])["max_queued"] == 2


def test_disable_member_awaits_cancel_before_granting_gpu(tmp_path):
    async def body():
        _, m = household(tmp_path)
        alice = AccountService(m).create(OWNER_USER_ID, ALICE, "Alice")
        sid = "alicerun01"
        ws = workspaces_dir(m.cfg, alice["user_id"]) / sid
        ws.mkdir(parents=True, exist_ok=True)
        now = 1_700_000_000.0
        m.db.insert_session({
            "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
            "title": sid, "status": "running", "workspace": str(ws), "created_at": now, "updated_at": now,
            "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": alice["user_id"],
        })
        started = asyncio.Event()
        still_running = asyncio.Event()

        async def fake_run():
            await m.scheduler.acquire(sid)
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                still_running.set()
                await asyncio.sleep(0.05)
                raise
            finally:
                m.scheduler.release(sid)

        m.tasks[sid] = asyncio.create_task(fake_run())
        await started.wait()
        owner = asyncio.create_task(m.scheduler.acquire("owner-run"))
        await asyncio.sleep(0.02)
        assert m.scheduler.holder == sid
        assert not owner.done()
        await m.disable_member(alice["user_id"])
        assert still_running.is_set()
        await asyncio.wait_for(owner, timeout=1)
        assert m.scheduler.holder == "owner-run"
        m.scheduler.release("owner-run")
    asyncio.run(body())


def test_isolated_refresh_origin_refuses_rewritten_origin(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice")
        uid = alice["user_id"]
        root = user_root(m.cfg, uid)
        src = repos_dir(m.cfg, uid) / "notes"
        src.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        (src / "README").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                       check=True)
        ws = workspaces_dir(m.cfg, uid) / "sessrf01"
        isolated_prepare(ws, src, "sessrf01", workspaces_dir(m.cfg, uid))
        assert isolated_refresh_origin(ws, root) == ""
        subprocess.run(["git", "-C", str(ws), "remote", "set-url", "origin", str(m.cfg.data_dir)], check=True)
        assert "account-local" in isolated_refresh_origin(ws, root)
        subprocess.run(["git", "-C", str(ws), "remote", "set-url", "origin",
                        "git@github.com:octocat/Hello-World.git"], check=True)
        assert "account-local" in isolated_refresh_origin(ws, root)


def test_isolated_refresh_origin_stops_when_fetch_exceeds_max_bytes(tmp_path):
    """A later fetch must not grow a member workspace past remaining quota (same class as clone caps)."""
    from harness.fileops import dir_size

    root = tmp_path / "user"
    src = root / "repos" / "notes"
    src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "small.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "small"],
                   check=True)
    dest = root / "workspaces" / "sessrf02"
    isolated_prepare(dest, src, "sessrf02", root / "workspaces")
    before = dir_size(dest)
    (src / "blob.bin").write_bytes(os.urandom(80_000))
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"],
                   check=True)
    err = isolated_refresh_origin(dest, root, max_bytes=before + 2_000)
    assert err, "uncapped fetch would grow the workspace by the new origin blob"
    assert "quota" in err.lower()
    assert dest.exists()
    assert (dest / "small.txt").exists()


def test_quota_ignores_links_and_cleanup_stays_contained(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice")
        root = ensure_user_dirs(m.cfg, alice["user_id"])
        owner_blob = m.cfg.workspaces_dir / "owner-secret.bin"
        owner_blob.parent.mkdir(parents=True, exist_ok=True)
        owner_blob.write_bytes(b"y" * 5000)
        link = root / "artifacts" / "escape"
        try:
            link.symlink_to(owner_blob)
            used = account_usage_bytes(m.cfg, alice["user_id"])
            assert used < 4000
        except OSError:
            pass

        sid = "keepdis01"
        ws = root / "workspaces" / sid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "notes.txt").write_text("keep", encoding="utf-8")
        now = 1_700_000_000.0
        m.db.insert_session({
            "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
            "title": sid, "status": "cancelled", "workspace": str(ws), "created_at": now, "updated_at": now,
            "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": alice["user_id"],
        })
        client.patch(f"{PREFIX}/accounts/{alice['user_id']}", json={"enabled": False}, headers=H(OWNER))
        assert ws.exists()
        planted = m.cfg.workspaces_dir / "owner-ws"
        planted.mkdir(parents=True, exist_ok=True)
        (planted / "secret.txt").write_text("owner", encoding="utf-8")
        m.db.update_session(sid, workspace=str(planted))
        m.maintenance.remove_workspace(sid)
        assert planted.exists()
        assert (planted / "secret.txt").read_text(encoding="utf-8") == "owner"


def test_scheduler_ineligible_waiters_do_not_block_eligible(tmp_path):
    """A skipped member waiter must not pin holder=None so later owner sessions never run."""
    async def body():
        eligible = {"blocked": False}
        s = GpuScheduler(eligible=lambda sid: eligible.get(sid, True))
        await s.acquire("running-member")
        blocked = asyncio.create_task(s.acquire("blocked"))
        await asyncio.sleep(0)
        s.release("running-member")
        assert s.holder is None and not blocked.done()
        await asyncio.wait_for(s.acquire("owner"), timeout=1)
        assert s.holder == "owner" and not blocked.done()
        s.release("owner")
        assert s.holder is None
        eligible["blocked"] = True
        s.recheck()
        await asyncio.wait_for(blocked, timeout=1)
        assert s.holder == "blocked"
        s.release("blocked")
    asyncio.run(body())


def test_scheduler_skips_ineligible_without_reordering_eligible(tmp_path):
    async def body():
        eligible = {"a": False, "b": True, "c": True}
        order = []
        s = GpuScheduler(eligible=lambda sid: eligible.get(sid, True))
        await s.acquire("holder")

        async def worker(sid):
            await s.acquire(sid)
            order.append(sid)
            await asyncio.sleep(0.01)
            s.release(sid)

        tasks = [asyncio.create_task(worker(x)) for x in "abc"]
        await asyncio.sleep(0.02)
        s.release("holder")
        await asyncio.sleep(0.05)
        assert order == ["b", "c"]
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        eligible["a"] = True
        await s.acquire("a")
        assert s.holder == "a"
        s.release("a")
    asyncio.run(body())


def test_scheduler_grants_non_session_gpu_holders(tmp_path):
    """Image/device holders are not household sessions and must not wait forever."""
    async def body():
        _, m = household(tmp_path)
        await m.scheduler.acquire("images")
        assert m.scheduler.holder == "images"
        m.scheduler.release("images")
    asyncio.run(body())


def test_scheduler_gpu_hold_preserves_global_fifo(tmp_path):
    async def body():
        order = []
        s = GpuScheduler()
        s.set_paused(True)

        async def worker(sid):
            await s.acquire(sid)
            order.append(sid)
            s.release(sid)

        tasks = [asyncio.create_task(worker(x)) for x in ("member-a", "owner-b")]
        await asyncio.sleep(0.02)
        assert s.holder is None
        s.set_paused(False)
        await asyncio.gather(*tasks)
        assert order == ["member-a", "owner-b"]
    asyncio.run(body())


def test_owner_accounts_ui_and_js_hide_member_content(tmp_path):
    client, _ = household(tmp_path)
    with client:
        js = client.get("/static/app.js").text
        assert "function isMember()" in js
        assert "accountsCard" in js
        assert "Household member" in js
        assert "function ownerSurface()" in js
        assert 'if (isMember()) return "app";' in js
        assert 'agentHarnessWeb.url("/events", ownerSurface())' in js
        accounts_js = js.split("async function accountsCard")[1].split("async function viewProfile")[0]
        assert "Delete" not in accounts_js
        create_member(client, ALICE, "Alice")
        rows = client.get(f"{PREFIX}/accounts", headers=H(OWNER)).json()
        assert rows[0]["display_name"] == "Alice"
        blob = str(rows)
        for leak in ("prompt", "transcript", "diff", "github.com"):
            assert leak not in blob
        assert client.get(f"{PREFIX}").json()["api_version"] == ADMIN_API_VERSION
        assert any(op["path"] == f"{PREFIX}/accounts" for op in client.get(PREFIX).json()["operations"])


def test_guest_stays_non_durable(tmp_path):
    guests = [GuestAccess(login=GUEST, until="2099-01-01T00:00:00+00:00")]
    client, m = household(tmp_path, guests=guests)
    with client:
        me = client.get("/me", headers=H(GUEST)).json()
        assert me["role"] == "guest" and me["user_id"] is None
        assert client.post("/sessions", json={"prompt": "x"}, headers=H(GUEST)).status_code == 403
        assert m.db.member_count() == 0
        assert client.post("/api/v1/projects", json={"name": "g"}, headers=H(GUEST)).status_code in (401, 403)


def test_no_secrets_in_sqlite(tmp_path):
    client, m = household(tmp_path)
    with client:
        create_member(client, ALICE, "Alice")
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"]},
                          headers=H(OWNER)).json()
        raw = m.cfg.db_path.read_bytes()
        assert app["key"].encode() not in raw
        assert b"Tailscale" not in raw
        accounts = list(m.db.list_accounts())
        assert "token" not in accounts[0]
        assert ALICE in accounts[0]["login"]


def test_member_git_clone_tool_refuses_private_and_local(tmp_path):
    async def body():
        root = tmp_path / "ws"
        root.mkdir()
        ws = Workspace(root, None, tmp_path / "repos", 8000, public_clone_only=True)
        for url in ("local:demo", "file:///tmp/x", "C:/secret", "https://evil.example/o/r",
                    "https://user:pass@github.com/o/r"):
            with pytest.raises(ToolError):
                await ws.git_clone(url)
    asyncio.run(body())


def test_member_git_clone_tool_stops_when_over_budget(tmp_path):
    async def body():
        src = tmp_path / "src"
        src.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        (src / "blob.bin").write_bytes(b"x" * 80_000)
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "big"],
                       check=True)
        root = tmp_path / "ws"
        root.mkdir()
        ws = Workspace(root, None, tmp_path / "repos", 8000, public_clone_only=True, clone_max_bytes=2_000)
        import harness.clone as clone_mod
        orig = clone_mod.public_https_url
        clone_mod.public_https_url = lambda url: str(src)
        try:
            with pytest.raises(ToolError, match="quota"):
                await ws.git_clone("https://github.com/octocat/Hello-World", dest="cloned")
        finally:
            clone_mod.public_https_url = orig
        assert not (root / "cloned").exists()
    asyncio.run(body())


def test_member_workspace_clone_budget_is_remaining_quota(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", disk_quota_bytes=100_000)
        uid = alice["user_id"]
        (repos_dir(m.cfg, uid) / "fat.bin").write_bytes(b"x" * 3_000)
        now = 1_700_000_000.0
        sid = "membudg01"
        ws_path = workspaces_dir(m.cfg, uid) / sid
        ws_path.mkdir(parents=True, exist_ok=True)
        row = {
            "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
            "title": sid, "status": "queued", "workspace": str(ws_path), "created_at": now, "updated_at": now,
            "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": uid,
        }
        remaining = 100_000 - account_usage_bytes(m.cfg, uid)
        ws = m.runner.workspace(row)
        assert ws.public_clone_only
        assert ws.clone_max_bytes == remaining
        owner_row = {**row, "owner_id": OWNER_USER_ID, "workspace": str(tmp_path / "owner-ws")}
        (tmp_path / "owner-ws").mkdir()
        owner_ws = m.runner.workspace(owner_row)
        assert not owner_ws.public_clone_only
        assert owner_ws.clone_max_bytes is None


def test_member_refresh_passes_remaining_quota_as_max_bytes(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", disk_quota_bytes=100_000)
        uid = alice["user_id"]
        sid = "membudg02"
        ws_path = workspaces_dir(m.cfg, uid) / sid
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "keep.txt").write_text("stay", encoding="utf-8")
        repo = repos_dir(m.cfg, uid) / "notes"
        repo.mkdir(parents=True, exist_ok=True)
        m.db.insert_member_project({
            "user_id": uid, "slug": "notes", "description": "", "repo": str(repo), "source_url": "",
        })
        now = 1_700_000_000.0
        m.db.insert_session({
            "id": sid, "project": "notes", "target": "tower", "model": "fake", "backend": "local",
            "title": sid, "status": "queued", "workspace": str(ws_path), "created_at": now, "updated_at": now,
            "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": uid,
            "base_commit": "abc123",
        })
        seen = {}

        def fake_refresh(workspace, root, max_bytes=None):
            seen["max_bytes"] = max_bytes
            seen["workspace"] = workspace
            return ""

        import harness.clone as clone_mod
        orig = clone_mod.isolated_refresh_origin
        clone_mod.isolated_refresh_origin = fake_refresh
        try:
            asyncio.run(m.runner._prepare_repo(m.db.get_session(sid)))
        finally:
            clone_mod.isolated_refresh_origin = orig
        from harness.fileops import dir_size
        remaining = 100_000 - account_usage_bytes(m.cfg, uid)
        assert seen["workspace"] == ws_path
        assert seen["max_bytes"] == remaining + dir_size(ws_path)


def test_member_approval_cannot_grant_owner_only_tool(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [OWNER]
    m = Manager(cfg, chat=Script([Completion(content="done")]))
    alice = AccountService(m).create(OWNER_USER_ID, ALICE, "Alice")
    now = 1_700_000_000.0
    sid = "membert001"
    ws_path = ensure_user_dirs(m.cfg, alice["user_id"]) / "workspaces" / sid
    ws_path.mkdir(parents=True, exist_ok=True)
    row = {
        "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local",
        "title": sid, "status": "running", "workspace": str(ws_path), "created_at": now, "updated_at": now,
        "context": [], "run": {}, "totals": {}, "inbox": [], "owner_id": alice["user_id"],
    }
    m.db.insert_session(row)
    m.db.insert_approval({
        "id": "a-fake01", "session_id": sid, "tool_call_id": "c1",
        "tool": "homelab_services", "args": {}, "reason": "injected",
    })
    assert m.db.decide_approval("a-fake01", "approved", "")
    ws = m.runner.workspace(row)
    out = asyncio.run(m.runner._authorize(row, {"id": "c1"}, "homelab_services", {}, ws))
    assert out and "cannot use that tool" in out
    names = {t["function"]["name"] for t in m.runner.tool_schemas(row, ws)}
    assert "homelab_services" not in names
