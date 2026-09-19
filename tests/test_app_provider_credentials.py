"""Issue #29: isolated owner-file provider credentials and per-app billing policy."""

from __future__ import annotations

import asyncio
import sys

from fastapi.testclient import TestClient

from harness.api import create_app
from harness.cli_backends import ClaudeSession
from harness.config import BackendConfig
from harness.manager import Manager
from test_daemon import make_cfg, wait_status
from test_phase8 import _claude_manager


def configured_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5")
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol")
    first, second = tmp_path / "first.key", tmp_path / "second.key"
    first.write_text("app-one-plaintext-secret", encoding="utf-8")
    second.write_text("app-two-plaintext-secret", encoding="utf-8")
    cfg.provider_secret_files = {"billing-one": str(first), "billing-two": str(second)}
    return Manager(cfg), first, second


def test_owner_assigns_opaque_refs_and_apps_only_see_their_policy(tmp_path, monkeypatch):
    from harness import backend_state
    monkeypatch.setattr(backend_state, "_subscription_status", lambda *_: True)
    manager, first, second = configured_manager(tmp_path)
    manager._spawn = lambda *_args, **_kwargs: None
    with TestClient(create_app(manager)) as client:
        app1 = client.post("/keys", json={"name": "one", "kind": "app", "scopes": ["sessions"]}).json()
        app2 = client.post("/keys", json={"name": "two", "kind": "app", "scopes": ["sessions"]}).json()
        one = client.post("/api/admin/v1/provider-credentials", json={
            "app_id": app1["id"], "backend": "claude", "secret_ref": "billing-one", "policy": "api_key",
            "models": ["claude-opus-5"],
        })
        two = client.post("/api/admin/v1/provider-credentials", json={
            "app_id": app2["id"], "backend": "claude", "secret_ref": "billing-two",
            "policy": "subscription_then_api_key", "models": ["claude-sonnet-5"],
        })
        assert one.status_code == two.status_code == 201
        owner_rows = client.get("/api/admin/v1/provider-credentials").json()
        assert {row["secret_ref"] for row in owner_rows} == {"billing-one", "billing-two"}
        assert "app-one-plaintext-secret" not in str(owner_rows) and str(first) not in str(owner_rows)

        h1, h2 = ({"Authorization": f"Bearer {app['key']}"} for app in (app1, app2))
        backends1 = {row["name"]: row for row in client.get("/api/v1/backends", headers=h1).json()}
        backends2 = {row["name"]: row for row in client.get("/api/v1/backends", headers=h2).json()}
        status1 = backends1["claude"]["provider_policy"]
        status2 = backends2["claude"]["provider_policy"]
        assert status1 == {"managed": True, "allowed": True, "policy": "api_key",
                           "models": ["claude-opus-5"], "credential_source": "app_file", "available": True}
        assert status2["policy"] == "subscription_then_api_key" and status2["models"] == ["claude-sonnet-5"]
        assert "secret_ref" not in status1 and "billing-two" not in str(status1)
        assert backends1["claude"]["logged_in"] and backends1["claude"]["api_key_available"]
        assert backends1["claude"]["limits"] == {}  # never another assignment's cached provider limits
        assert not backends1["codex"]["available"] and not backends1["codex"]["api_key_available"]

        assert client.post("/api/v1/sessions", headers=h1,
                           json={"prompt": "x", "backend": "codex"}).status_code == 403
        assert client.post("/api/v1/sessions", headers=h1,
                           json={"prompt": "x", "backend": "claude", "model": "claude-haiku"}).status_code == 403
        allowed = client.post("/api/v1/sessions", headers=h1,
                              json={"prompt": "x", "backend": "claude", "model": "claude-opus-5"})
        assert allowed.status_code == 201

        # An app token cannot inspect the owner-only assignment registry.
        assert client.get("/api/admin/v1/provider-credentials", headers=h1).status_code == 403

        # Revocation locks future provider use without exposing or affecting another app's assignment.
        assert client.delete(f"/api/admin/v1/provider-credentials/{two.json()['id']}").status_code == 204
        revoked = {row["name"]: row for row in client.get("/api/v1/backends", headers=h2).json()}["claude"]
        assert not revoked["available"] and revoked["provider_policy"]["managed"]
        assert revoked["provider_policy"]["allowed"] is False
        assert client.post("/api/v1/sessions", headers=h2,
                           json={"prompt": "x", "backend": "claude", "model": "claude-sonnet-5"}).status_code == 403
        assert backends1["claude"]["provider_policy"]["allowed"] is True

    stored = manager.db.conn.execute(
        "SELECT secret_ref, models FROM app_provider_credentials WHERE app_id = ?", (app1["id"],)
    ).fetchone()
    assert tuple(stored) == ("billing-one", '["claude-opus-5"]')
    # Scan the main DB and its WAL: neither plaintext nor owner-local file paths may be durable.
    durable = b"".join(path.read_bytes() for path in manager.cfg.db_path.parent.glob(manager.cfg.db_path.name + "*"))
    assert b"app-one-plaintext-secret" not in durable and str(first).encode() not in durable
    assert b"app-two-plaintext-secret" not in durable and str(second).encode() not in durable


def test_revoked_default_backend_drops_its_model_and_falls_back_to_local(tmp_path):
    manager, _, _ = configured_manager(tmp_path)
    manager._spawn = lambda *_args, **_kwargs: None
    with TestClient(create_app(manager)) as client:
        app = client.post("/keys", json={
            "name": "builder", "kind": "app", "scopes": ["sessions"],
        }).json()
        credential = manager.set_app_provider_credential(
            app["id"], "claude", "billing-one", "api_key", ["claude-sonnet-4"],
        )
        headers = {"Authorization": f"Bearer {app['key']}"}
        patched = client.patch("/api/v1/config", headers=headers, json={
            "revision": 0,
            "changes": {
                "app.default_backend": "claude",
                "app.default_model": "claude-sonnet-4",
            },
        })
        assert patched.status_code == 200, patched.text

        assert manager.revoke_app_provider_credential(credential["id"])
        effective = {row["key"]: row["effective"] for row in client.get(
            "/api/v1/config", headers=headers,
        ).json()["settings"]}
        assert effective["app.default_backend"] == ""
        assert effective["app.default_model"] == ""

        created = client.post("/api/v1/sessions", headers=headers, json={"prompt": "hello"})
        assert created.status_code == 201, created.text
        session = manager.db.get_session(created.json()["id"])
        assert session["backend"] == "local"
        assert session["model"] == manager.cfg.default_model


def test_app_file_key_is_used_and_usage_is_attributed_without_storing_it(tmp_path):
    async def body():
        manager, made, _ = _claude_manager(tmp_path, "echo")
        secret_file = tmp_path / "app.key"
        secret_file.write_text("isolated-app-secret", encoding="utf-8")
        manager.cfg.provider_secret_files = {"app-billing": str(secret_file)}
        app, _token = manager.db.create_api_key("builder", "sessions", "app")
        manager.set_app_provider_credential(app["id"], "claude", "app-billing", "api_key", ["claude-opus-5"])
        await manager.start()
        sid = manager.create("use my billing", backend="claude", app=app)["id"]
        session = await wait_status(manager, sid, "done")
        await asyncio.gather(*manager.tasks.values())
        assert made[0]["api_key"] == "isolated-app-secret"
        assert session["run"]["credential_source"] == "app_file"
        usage = manager.db.conn.execute(
            "SELECT app_id, billing, credential_source FROM usage WHERE session_id = ?", (sid,)).fetchone()
        assert tuple(usage) == (app["id"], "api_key", "app_file")
        assert manager.db.usage_tally("claude", 0, app["id"])["requests"] == 1
        assert manager.db.usage_by_source("claude", 0, app["id"])["app_file"]["requests"] == 1
        await manager.stop()
        manager.db.close()
        raw = manager.cfg.db_path.read_bytes()
        assert b"isolated-app-secret" not in raw and str(secret_file).encode() not in raw
    asyncio.run(body())


def test_revoking_assignment_stops_an_active_provider_process(tmp_path):
    async def body():
        manager, _, _ = _claude_manager(tmp_path, "cancel")
        secret_file = tmp_path / "app.key"
        secret_file.write_text("temporary-secret", encoding="utf-8")
        manager.cfg.provider_secret_files = {"temporary": str(secret_file)}
        app, _token = manager.db.create_api_key("builder", "sessions", "app")
        credential = manager.set_app_provider_credential(app["id"], "claude", "temporary", "api_key", [])
        await manager.start()
        sid = manager.create("keep running", backend="claude", app=app)["id"]
        await wait_status(manager, sid, "running")
        assert manager.revoke_app_provider_credential(credential["id"])
        failed = await wait_status(manager, sid, "failed")
        assert failed["run"]["failure"]["code"] == "provider_auth_required"
        await asyncio.gather(*manager.tasks.values())
        assert sid not in manager.runner._cli_sessions
        await manager.stop()
    asyncio.run(body())


def test_app_subscription_limit_falls_back_only_to_its_assigned_key(tmp_path):
    async def body():
        manager, made, state = _claude_manager(tmp_path, "limit")
        secret_file = tmp_path / "fallback.key"
        secret_file.write_text("per-app-fallback", encoding="utf-8")
        manager.cfg.provider_secret_files = {"app-fallback": str(secret_file)}
        app, _token = manager.db.create_api_key("builder", "sessions", "app")
        manager.set_app_provider_credential(app["id"], "claude", "app-fallback",
                                            "subscription_then_api_key", [])

        def factory(**kwargs):
            made.append(kwargs)
            mode = "echo" if kwargs.get("api_key") else "limit"
            return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(tmp_path / "fake_claude.py"),
                                                    mode, str(state), "tool-1"])

        manager.runner.cli_factory = factory
        await manager.start()
        sid = manager.create("fall back", backend="claude", app=app)["id"]
        session = await wait_status(manager, sid, "done")
        assert len(made) == 2 and made[0]["api_key"] == "" and made[1]["api_key"] == "per-app-fallback"
        assert session["run"]["billing_mode"] == "api_key"
        assert session["run"]["credential_source"] == "app_file"
        row = manager.db.conn.execute(
            "SELECT app_id, billing, credential_source FROM usage WHERE session_id = ?", (sid,)
        ).fetchone()
        assert tuple(row) == (app["id"], "api_key", "app_file")
        await manager.stop()
    asyncio.run(body())
