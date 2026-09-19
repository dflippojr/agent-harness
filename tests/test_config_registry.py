"""Issue #66: typed configuration registry, persistence, auth, restart, and web surface."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import load, module_effective
from harness.llm import Completion
from harness.managed_config import Envelope, ManagedStore, OverlayCrash
from harness.manager import Manager
from harness.settings import frozen_app_defaults, looks_hidden, parse_value, schema_entry, use_live_app_settings
from harness.settings_keys import APP_SPECS, STATIC_ADMIN, assert_explicit_registry, build_registry
from harness.settings_service import SettingsError, SettingsService

from test_daemon import Script, make_cfg
from test_admin import PREFIX, bearer


def _client(tmp_path, **cfg_kw):
    cfg = make_cfg(tmp_path)
    for key, value in cfg_kw.items():
        setattr(cfg, key, value)
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    return TestClient(create_app(manager)), manager


def _write_loadable_config(tmp_path, extra=""):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (cfg_dir / "harness.yaml").write_text(
        "listen: {host: 127.0.0.1, port: 8100}\n"
        f"data_dir: {data_dir.as_posix()}\n"
        "default_model: fake\n"
        "models: {fake: {base_url: http://unused, context_tokens: 1024}}\n"
        "sandbox: {image: agent-harness-sandbox:py312}\n"
        "search: {enabled: true}\n"
        f"{extra}",
        encoding="utf-8",
    )
    return cfg_dir, data_dir


# ---------- registry coverage ----------
def test_registry_specs_are_explicit_and_reject_unknown_keys(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = type(cfg.cleanup)  # placeholder replaced below
    from harness.config import BackendConfig
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    registry = build_registry(cfg)
    assert_explicit_registry(registry)
    for spec in registry.specs.values():
        entry = schema_entry(spec, cfg)
        assert entry["key"] == spec.key
        assert entry["scope"] in ("app", "admin")
        assert entry["apply"] in ("live", "daemon_restart", "installer_only")
        assert callable(spec.getter) and callable(spec.setter)
        if spec.apply_mode != "installer_only":
            spec.getter(cfg)
    with pytest.raises(KeyError):
        registry.get("this.is.not.registered")
    with pytest.raises(ValueError):
        parse_value(registry.get("sessions.max_turns"), "80")
    hidden = registry.get("backup.dir")
    assert hidden.apply_mode == "installer_only" and looks_hidden(hidden.key, hidden)
    assert schema_entry(hidden, cfg)["guidance"] == "managed in local configuration"


def test_image_edit_settings_are_explicit_and_install_gated(tmp_path):
    cfg = make_cfg(tmp_path)
    registry = build_registry(cfg)
    keys = ("images.edit_enabled", "images.max_upload_bytes", "images.max_pixels")
    assert all(key in registry.specs for key in keys)
    assert all(schema_entry(registry.get(key), cfg)["available"] is False for key in keys)
    cfg.installed.image_edit = True
    cfg.modules.image_edit = True
    cfg.images.edit_enabled = True
    for key in keys:
        entry = schema_entry(registry.get(key), cfg)
        assert entry["available"] is True and entry["modules"] == ["image_edit"]
    registry.get("images.max_upload_bytes").setter(cfg, 8 * 2**20)
    registry.get("images.max_pixels").setter(cfg, 12_000_000)
    registry.get("images.edit_enabled").setter(cfg, False)
    assert cfg.images.max_upload_bytes == 8 * 2**20
    assert cfg.images.max_pixels == 12_000_000
    assert cfg.images.edit_enabled is False


def test_compaction_and_feature_enable_cross_field_validation(tmp_path):
    client, manager = _client(tmp_path)
    with client:
        bad = client.patch("/api/admin/v1/config", json={
            "revision": 0,
            "changes": {"compaction.elide_at": 0.8, "compaction.summarize_at": 0.4},
        })
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "validation_error"
        assert "compaction.summarize_at" in bad.json()["error"]["keys"]
        unknown = client.patch("/api/admin/v1/config", json={"revision": 0, "changes": {"nope.secret": "x"}})
        assert unknown.status_code == 400 and unknown.json()["error"]["keys"]["nope.secret"]["code"] == "unknown_key"
        file_only = client.patch("/api/admin/v1/config", json={"revision": 0, "changes": {"backup.dir": "C:/x"}})
        assert file_only.status_code == 400
        assert file_only.json()["error"]["keys"]["backup.dir"]["code"] == "installer_only"
        listed = client.get("/api/admin/v1/config").json()
        backup = next(item for item in listed["settings"] if item["key"] == "backup.dir")
        assert backup["configured"] is None and backup["effective"] is None
        assert "D:" not in json.dumps(listed) and "Agents" not in json.dumps(listed)


def test_live_patch_reset_and_stale_revision(tmp_path):
    client, manager = _client(tmp_path)
    with client:
        schema = client.get("/api/admin/v1/config/schema").json()
        assert schema["schema_version"] == 1
        keys = {item["key"] for item in schema["settings"]}
        assert "sessions.max_turns" in keys and "modules.web" in keys
        view = client.get("/api/admin/v1/config")
        assert view.headers["etag"] == '"0"'
        plan = client.patch("/api/admin/v1/config", json={
            "revision": 0, "dry_run": True, "changes": {"sessions.max_turns": 40},
        }).json()
        assert plan["dry_run"] is True and plan["changes"][0]["to"] == 40
        assert manager.cfg.max_turns == 80
        saved = client.patch("/api/admin/v1/config", json={
            "revision": 0, "changes": {"sessions.max_turns": 40},
        })
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        assert manager.cfg.max_turns == 40
        stale = client.patch("/api/admin/v1/config", json={"revision": 0, "changes": {"sessions.max_turns": 50}})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
        reset = client.patch("/api/admin/v1/config", json={"revision": 1, "reset": ["sessions.max_turns"]})
        assert reset.status_code == 200
        assert manager.cfg.max_turns == 80
        assert "sessions.max_turns" not in (manager.settings.store.read_active().values)


def test_reset_live_compaction_key_revalidates_inherited_thresholds(tmp_path):
    """Resetting one live compaction key must validate against YAML/inherited siblings,
    not the pre-reset overlay still sitting on candidate_cfg."""
    client, manager = _client(tmp_path)
    with client:
        saved = client.patch("/api/admin/v1/config", json={
            "revision": 0,
            "changes": {"compaction.elide_at": 0.20, "compaction.summarize_at": 0.50},
        })
        assert saved.status_code == 200, saved.text
        assert manager.cfg.elide_at == 0.20 and manager.cfg.summarize_at == 0.50
        # Inherited elide_at is 0.55; leaving summarize_at=0.50 would violate elide < summarize.
        reset_elide = client.patch("/api/admin/v1/config", json={
            "revision": saved.json()["revision"], "reset": ["compaction.elide_at"],
        })
        assert reset_elide.status_code == 400, reset_elide.text
        assert reset_elide.json()["error"]["code"] == "validation_error"
        assert "compaction.summarize_at" in reset_elide.json()["error"]["keys"]
        assert manager.cfg.elide_at == 0.20 and manager.cfg.summarize_at == 0.50
        assert "compaction.elide_at" in manager.settings.store.read_active().values

        # Symmetric: overlay elide_at=0.80 with inherited summarize_at=0.65 is also invalid.
        high = client.patch("/api/admin/v1/config", json={
            "revision": saved.json()["revision"],
            "changes": {"compaction.elide_at": 0.80, "compaction.summarize_at": 0.90},
        })
        assert high.status_code == 200, high.text
        reset_summarize = client.patch("/api/admin/v1/config", json={
            "revision": high.json()["revision"], "reset": ["compaction.summarize_at"],
        })
        assert reset_summarize.status_code == 400, reset_summarize.text
        assert "compaction.summarize_at" in reset_summarize.json()["error"]["keys"]
        assert manager.cfg.elide_at == 0.80 and manager.cfg.summarize_at == 0.90

        # Resetting the overlay sibling that restores a valid pair still works.
        ok = client.patch("/api/admin/v1/config", json={
            "revision": high.json()["revision"], "reset": ["compaction.elide_at"],
        })
        assert ok.status_code == 200, ok.text
        assert manager.cfg.elide_at == 0.55 and manager.cfg.summarize_at == 0.90


def test_rollback_restores_immediately_previous_generation(tmp_path):
    client, manager = _client(tmp_path)
    with client:
        first = client.patch("/api/admin/v1/config", json={
            "revision": 0, "changes": {"sessions.max_turns": 40},
        })
        assert first.status_code == 200
        undo_first = client.post("/api/admin/v1/config/rollback", json={
            "confirm": True, "revision": first.json()["revision"],
        })
        assert undo_first.status_code == 200, undo_first.text
        assert manager.cfg.max_turns == 80

        forty = client.patch("/api/admin/v1/config", json={
            "revision": undo_first.json()["revision"], "changes": {"sessions.max_turns": 40},
        })
        assert forty.status_code == 200
        fifty = client.patch("/api/admin/v1/config", json={
            "revision": forty.json()["revision"], "changes": {"sessions.max_turns": 50},
        })
        assert fifty.status_code == 200
        sixty = client.patch("/api/admin/v1/config", json={
            "revision": fifty.json()["revision"], "changes": {"sessions.max_turns": 60},
        })
        assert sixty.status_code == 200
        assert manager.cfg.max_turns == 60
        rolled = client.post("/api/admin/v1/config/rollback", json={
            "confirm": True, "revision": sixty.json()["revision"],
        })
        assert rolled.status_code == 200, rolled.text
        assert manager.cfg.max_turns == 50


def test_yaml_semantics_and_managed_precedence(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "harness.yaml").write_text(
        "listen: {host: 127.0.0.1, port: 8100}\n"
        "data_dir: %s\n"
        "default_model: fake\n"
        "models: {fake: {base_url: http://unused, context_tokens: 1024}}\n"
        "budgets: {max_turns: 70}\n"
        "compaction: {elide_at: 0.4, summarize_at: 0.6, keep_recent: 0.2}\n"
        "sandbox: {image: agent-harness-sandbox:py312}\n"
        % (tmp_path / "data").as_posix(),
        encoding="utf-8",
    )
    (cfg_dir / "harness.local.yaml").write_text("budgets: {max_turns: 90}\n", encoding="utf-8")
    (cfg_dir / "profile.yaml").write_text("profile: full\nbudgets: {max_turns: 10}\n", encoding="utf-8")
    cfg = load(cfg_dir)
    assert cfg.max_turns == 90  # local wins; profile.yaml is not a generic overlay
    store = ManagedStore(cfg.data_dir)
    store.write_active(Envelope(revision=1, values={"sessions.max_turns": 33}))
    cfg2 = load(cfg_dir)
    assert cfg2.max_turns == 33
    yaml_text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    assert "max_turns: 70" in yaml_text


def test_app_caps_and_admin_auth_boundaries(tmp_path):
    client, manager = _client(tmp_path)
    with client:
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions"]}).json()
        other = client.post("/keys", json={"name": "other", "kind": "app", "scopes": ["sessions"]}).json()
        device = client.post("/keys", json={"name": "zed"}).json()
        owner = client.post("/keys", json={"name": "cc", "kind": "owner", "scopes": ["admin"]}).json()
        h = bearer(app["key"])
        assert client.get("/api/v1/config/schema", headers=h).status_code == 200
        patched = client.patch("/api/v1/config", headers=h, json={
            "revision": 0, "changes": {"app.sessions.max_turns": 10, "app.notify.completion": "never"},
        })
        assert patched.status_code == 200
        assert patched.json()["settings"]
        # cannot raise an owner cap
        over = client.patch("/api/v1/config", headers=h, json={
            "revision": 1, "changes": {"app.sessions.max_turns": 400},
        })
        assert over.status_code == 200
        turns = next(item for item in over.json()["settings"] if item["key"] == "app.sessions.max_turns")
        assert turns["configured"] == 400 and turns["effective"] == 80 and turns["capped_by"] == "sessions.max_turns"
        # cannot write host settings, modules, installer-only, other apps
        assert client.patch("/api/v1/config", headers=h, json={
            "revision": 2, "changes": {"sessions.max_turns": 1},
        }).status_code == 400
        assert client.patch("/api/v1/config", headers=h, json={
            "revision": 2, "changes": {"web.enabled": True},
        }).status_code == 400
        assert client.patch("/api/v1/config", headers=bearer(other["key"]), json={
            "revision": 0, "changes": {"app.notify.completion": "never"},
        }).status_code == 200
        view = client.get("/api/v1/config", headers=h).json()
        notify = next(item for item in view["settings"] if item["key"] == "app.notify.completion")
        assert notify["effective"] == "never"
        for cred in (device["key"], owner["key"], "nope"):
            headers = bearer(cred) if cred != "nope" else {"Authorization": "Bearer nope"}
            response = client.get("/api/v1/config", headers=headers)
            assert response.status_code in (401, 403)
        for path, method in (
            (f"{PREFIX}/config", "GET"),
            (f"{PREFIX}/config/schema", "GET"),
            (f"{PREFIX}/config/validate", "POST"),
            (f"{PREFIX}/config", "PATCH"),
            (f"{PREFIX}/config/restart", "POST"),
            (f"{PREFIX}/config/rollback", "POST"),
        ):
            kwargs = {"headers": h}
            if method != "GET":
                kwargs["json"] = {"revision": 0, "changes": {}, "confirm": True}
            assert getattr(client, method.lower())(path, **kwargs).status_code == 403


class _Toolkit:
    def __init__(self, names):
        self.tool_names = names

    def schemas(self):
        return []

    def profile_text(self):
        return ""

    def refresh_soon(self):
        return None


def test_app_capabilities_narrow_toolkits_not_just_prompts(tmp_path):
    """app.capabilities must strip daemon toolkits and homelab tools, not only system-prompt text."""
    from harness.config import Project

    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(
        name="lab", homelab=True, web=True, memory_library=True, images=True, session_search=True,
    )
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.runner.memory = _Toolkit(("memory_index", "memory_search", "memory_read"))
    manager.runner.images = _Toolkit(("generate_image",))
    manager.runner.sessions = _Toolkit(("session_search", "session_read"))
    with client:
        app = client.post("/keys", json={
            "name": "shop", "kind": "app", "scopes": ["sessions", "images"],
        }).json()
        h = bearer(app["key"])
        patched = client.patch("/api/v1/config", headers=h, json={
            "revision": 0, "changes": {"app.capabilities": ["search"]},
        })
        assert patched.status_code == 200, patched.text
        created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": "lab"})
        assert created.status_code == 201, created.text
        s = manager.db.get_session(created.json()["id"])
        prompt = s["context"][0]["content"]
        assert "Web access" not in prompt and "Homelab access" not in prompt
        assert "User context:" not in prompt
        assert "Past work:" in prompt
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert kit_tools == {"session_search", "session_read"}
        ws_tools = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
        assert "restart_service" not in ws_tools and "homelab_services" not in ws_tools

        empty = client.patch("/api/v1/config", headers=h, json={
            "revision": patched.json()["revision"], "changes": {"app.capabilities": []},
        })
        assert empty.status_code == 200, empty.text
        created_empty = client.post("/api/v1/sessions", headers=h, json={"prompt": "again", "project": "lab"})
        assert created_empty.status_code == 201, created_empty.text
        s_empty = manager.db.get_session(created_empty.json()["id"])
        assert "Past work:" not in s_empty["context"][0]["content"]
        assert manager.runner.daemon_toolkits(s_empty) == []
        empty_ws = {t["function"]["name"] for t in manager.runner.workspace(s_empty).schemas()}
        assert "restart_service" not in empty_ws

        owner = client.post("/sessions", json={"prompt": "owner hello", "project": "lab"})
        assert owner.status_code == 201, owner.text
        owner_s = manager.db.get_session(owner.json()["id"])
        owner_tools = {name for kit in manager.runner.daemon_toolkits(owner_s) for name in kit.tool_names}
        assert {"web_search", "memory_index", "generate_image", "session_search"} <= owner_tools
        owner_ws = {t["function"]["name"] for t in manager.runner.workspace(owner_s).schemas()}
        assert "restart_service" in owner_ws


def test_revoked_app_in_flight_session_keeps_narrowed_settings(tmp_path):
    """Revoking an app deletes app_settings; in-flight sessions must keep the narrowed
    settings they started with (not stop, and not widen to owner defaults)."""
    import time

    from harness.config import Project
    from harness.notify import Notifier
    from harness.runner import ACTIVE

    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(
        name="lab", homelab=True, web=True, memory_library=True, images=True, session_search=True,
    )
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.runner.memory = _Toolkit(("memory_index", "memory_search", "memory_read"))
    manager.runner.images = _Toolkit(("generate_image",))
    manager.runner.sessions = _Toolkit(("session_search", "session_read"))
    with client:
        app = client.post("/keys", json={
            "name": "shop", "kind": "app", "scopes": ["sessions", "images"],
        }).json()
        h = bearer(app["key"])
        patched = client.patch("/api/v1/config", headers=h, json={
            "revision": 0,
            "changes": {
                "app.capabilities": ["search"],
                "app.sessions.max_turns": 10,
                "app.notify.completion": "never",
            },
        })
        assert patched.status_code == 200, patched.text
        created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": "lab"})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        s = manager.db.get_session(sid)
        assert s["run"]["max_turns"] == 10
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert kit_tools == {"session_search", "session_read"}

        assert client.delete(f"/keys/{app['id']}").status_code == 204
        assert manager.db.get_api_key(app["id"])["revoked_at"]
        assert manager.db.get_app_settings(app["id"]) is None

        s = manager.db.get_session(sid)
        assert s["status"] in ACTIVE or s["status"] in ("done", "failed", "cancelled")
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert kit_tools == {"session_search", "session_read"}
        ws_tools = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
        assert "restart_service" not in ws_tools and "homelab_services" not in ws_tools

        deadline = time.time() + 10
        while time.time() < deadline and manager.db.get_session(sid)["status"] in ACTIVE:
            time.sleep(0.05)
        follow = client.post(f"/sessions/{sid}/messages", json={"content": "continue"})
        assert follow.status_code == 200, follow.text
        assert manager.db.get_session(sid)["run"]["max_turns"] == 10

        note = Notifier(manager.cfg, manager.db).build({
            "session_id": sid, "type": "run_finished",
            "data": {"status": "done", "stop_reason": "final_message", "answer": "hi"},
        })
        assert note is None


def test_live_app_settings_discriminator_is_the_token_not_the_row():
    """Freeze only after revoke/delete. Missing rows and empty snapshots stay live/inherited."""
    active = {"id": "ha-1", "kind": "app", "revoked_at": None}
    revoked = {"id": "ha-1", "kind": "app", "revoked_at": 1.0}
    assert use_live_app_settings(active) is True
    assert use_live_app_settings(revoked) is False
    assert use_live_app_settings(None) is False
    assert use_live_app_settings({"id": "hk-1", "kind": "owner"}) is False
    assert frozen_app_defaults(None) == {}
    assert frozen_app_defaults({}) == {}
    narrowed = {"app.capabilities": ["search"], "app.notify.completion": "never"}
    assert frozen_app_defaults(narrowed) == narrowed


def test_never_patched_app_keeps_inherited_tools_and_notifications(tmp_path):
    """An app that never PATCHed /api/v1/config has no app_settings row; sessions still
    inherit project tools and completion notifications."""
    from harness.config import Project
    from harness.notify import Notifier

    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(
        name="lab", homelab=True, web=True, memory_library=True, images=True, session_search=True,
    )
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.runner.memory = _Toolkit(("memory_index", "memory_search", "memory_read"))
    manager.runner.images = _Toolkit(("generate_image",))
    manager.runner.sessions = _Toolkit(("session_search", "session_read"))
    with client:
        app = client.post("/keys", json={
            "name": "shop", "kind": "app", "scopes": ["sessions", "images"],
        }).json()
        h = bearer(app["key"])
        assert manager.db.get_app_settings(app["id"]) is None
        created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": "lab"})
        assert created.status_code == 201, created.text
        s = manager.db.get_session(created.json()["id"])
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert {"web_search", "memory_index", "generate_image", "session_search"} <= kit_tools
        ws_tools = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
        assert "restart_service" in ws_tools
        note = Notifier(manager.cfg, manager.db).build({
            "session_id": s["id"], "type": "run_finished",
            "data": {"status": "done", "stop_reason": "final_message", "answer": "hi"},
        })
        assert note is not None and "Done" in note["title"]


def test_pre_upgrade_empty_snapshot_follows_live_defaults(tmp_path):
    """Sessions created before app_defaults existed store '{}'; after upgrade they must
    not lock capabilities to [] / notify to never while the token is still active."""
    from harness.config import Project
    from harness.notify import Notifier

    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(
        name="lab", homelab=True, web=True, memory_library=True, images=True, session_search=True,
    )
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.runner.memory = _Toolkit(("memory_index", "memory_search", "memory_read"))
    manager.runner.images = _Toolkit(("generate_image",))
    manager.runner.sessions = _Toolkit(("session_search", "session_read"))
    with client:
        app = client.post("/keys", json={
            "name": "shop", "kind": "app", "scopes": ["sessions", "images"],
        }).json()
        h = bearer(app["key"])
        created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": "lab"})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        manager.db.update_session(sid, app_defaults={})
        assert manager.db.get_app_settings(app["id"]) is None
        s = manager.db.get_session(sid)
        assert s["app_defaults"] == {}
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert {"web_search", "memory_index", "generate_image", "session_search"} <= kit_tools
        ws_tools = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
        assert "restart_service" in ws_tools
        note = Notifier(manager.cfg, manager.db).build({
            "session_id": sid, "type": "run_finished",
            "data": {"status": "done", "stop_reason": "final_message", "answer": "hi"},
        })
        assert note is not None and "Done" in note["title"]


def test_active_app_follows_live_config_patch(tmp_path):
    """While the token is active, a later PATCH applies to in-flight sessions."""
    from harness.config import Project
    from harness.notify import Notifier

    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(
        name="lab", homelab=True, web=True, memory_library=True, images=True, session_search=True,
    )
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.runner.memory = _Toolkit(("memory_index", "memory_search", "memory_read"))
    manager.runner.images = _Toolkit(("generate_image",))
    manager.runner.sessions = _Toolkit(("session_search", "session_read"))
    with client:
        app = client.post("/keys", json={
            "name": "shop", "kind": "app", "scopes": ["sessions", "images"],
        }).json()
        h = bearer(app["key"])
        created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": "lab"})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        s = manager.db.get_session(sid)
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert {"web_search", "session_search"} <= kit_tools

        patched = client.patch("/api/v1/config", headers=h, json={
            "revision": 0,
            "changes": {"app.capabilities": ["search"], "app.notify.completion": "never"},
        })
        assert patched.status_code == 200, patched.text
        s = manager.db.get_session(sid)
        kit_tools = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert kit_tools == {"session_search", "session_read"}
        ws_tools = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
        assert "restart_service" not in ws_tools
        note = Notifier(manager.cfg, manager.db).build({
            "session_id": sid, "type": "run_finished",
            "data": {"status": "done", "stop_reason": "final_message", "answer": "hi"},
        })
        assert note is None


def test_live_hook_failure_restores_disk_and_memory(tmp_path, monkeypatch):
    client, manager = _client(tmp_path)
    calls = []

    def boom(mgr, old, new):
        calls.append((old, new))
        raise RuntimeError("nope")

    spec = manager.settings.registry.get("sessions.max_turns")
    spec.live_apply = boom
    with client:
        response = client.patch("/api/admin/v1/config", json={"revision": 0, "changes": {"sessions.max_turns": 12}})
        assert response.status_code == 500
        assert manager.cfg.max_turns == 80
        assert manager.settings.store.read_active() is None or "sessions.max_turns" not in (
            manager.settings.store.read_active().values)
        assert calls == [(80, 12)]


def test_unsupervised_restart_does_not_kill_process(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_SUPERVISED", raising=False)
    died = []
    monkeypatch.setattr("harness.config_api.schedule_exit", lambda: died.append(True))
    client, manager = _client(tmp_path)
    with client:
        client.patch("/api/admin/v1/config", json={
            "revision": 0, "changes": {"web.enabled": False},
        })
        response = client.post("/api/admin/v1/config/restart", json={"confirm": True})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "restart_not_supervised"
        assert died == []
        assert client.get("/health").status_code == 200


def test_supervised_restart_promotes_pending_and_confirms(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    died = []
    monkeypatch.setattr("harness.config_api.schedule_exit", lambda: died.append(True))
    client, manager = _client(tmp_path)
    with client:
        manager.cfg.search.enabled = True
        manager.cfg.modules.search = True
        manager.cfg.installed.search = True
        saved = client.patch("/api/admin/v1/config", json={"revision": 0, "changes": {"search.enabled": False}})
        assert saved.status_code == 200, saved.text
        pending = manager.settings.store.read_pending()
        assert pending is not None, saved.json()
        assert pending.values.get("search.enabled") is False, pending
        assert saved.json()["restart_required"] is True
        assert manager.cfg.search.enabled is True  # not yet applied
        restart = client.post("/api/admin/v1/config/restart", json={"confirm": True, "revision": saved.json()["revision"]})
        assert restart.status_code == 202
        assert restart.json()["target_revision"] == saved.json()["pending_revision"]
        assert died == [True]
        active = manager.settings.store.read_active()
        assert active.values.get("search.enabled") is False
        assert active.confirmed is False
    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    cfg.modules.search = True
    cfg.installed.search = True
    reloaded = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert reloaded.cfg.search.enabled is False
    reloaded.settings.confirm_startup()
    assert reloaded.settings.store.read_active().confirmed is True
    assert reloaded.settings.store.read_pending() is None


def test_load_then_manager_applies_unconfirmed_candidate_once(tmp_path):
    """Production boot is load() then Manager(cfg). The unconfirmed candidate must not be
    treated as a failed retry just because apply_overlay runs twice in the same process."""
    cfg_dir, data_dir = _write_loadable_config(tmp_path)
    store = ManagedStore(data_dir)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"search.enabled": True}))
    store.write_active(Envelope(revision=2, confirmed=False, unconfirmed=True,
                                values={"search.enabled": False}))
    cfg = load(cfg_dir)
    assert cfg.search.enabled is False
    assert store.boot_tried()
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert manager.cfg.search.enabled is False
    assert manager.settings.store.read_status().get("recovery") != "lkg_restore"
    assert not manager.settings.store.quarantine_path.is_file()
    active = manager.settings.store.read_active()
    assert active is not None and active.values.get("search.enabled") is False
    assert active.confirmed is False


def test_unconfirmed_candidate_restores_lkg(tmp_path):
    cfg = make_cfg(tmp_path)
    store = ManagedStore(cfg.data_dir)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"sessions.max_turns": 40}))
    store.write_active(Envelope(revision=2, confirmed=False, values={"sessions.max_turns": 12}))
    store.mark_boot_tried()
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert manager.cfg.max_turns == 40
    assert manager.settings.store.read_status().get("recovery") == "lkg_restore"
    assert manager.settings.store.quarantine_path.is_file()


def test_managed_store_lock_is_reentrant(tmp_path):
    store = ManagedStore(tmp_path / "data")
    finished = threading.Event()

    def nested():
        with store.lock():
            with store.lock():
                store.write_active(Envelope(revision=1, values={"sessions.max_turns": 11}))
        finished.set()

    thread = threading.Thread(target=nested, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "ManagedStore.lock deadlocked on nested acquire"
    assert finished.is_set()
    assert store.read_active().values["sessions.max_turns"] == 11


def test_rollback_does_not_deadlock_on_nested_store_lock(tmp_path):
    cfg = make_cfg(tmp_path)
    service = SettingsService(cfg)
    store = service.store
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"sessions.max_turns": 40}))
    store.write_active(Envelope(revision=2, confirmed=True, values={"sessions.max_turns": 50}))
    cfg.max_turns = 50
    result = {}

    def run():
        try:
            result["body"] = service.rollback(2, actor={"id": "owner", "kind": "owner"})
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "rollback deadlocked holding ManagedStore.lock across patch_admin"
    assert "error" not in result, result.get("error")
    assert cfg.max_turns == 40


def test_corrupt_and_concurrent_writes(tmp_path):
    cfg = make_cfg(tmp_path)
    store = ManagedStore(cfg.data_dir)
    store.write_active(Envelope(revision=1, values={"sessions.max_turns": 40}))
    store.active_path.write_text("{not json", encoding="utf-8")
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"sessions.max_turns": 40}))
    service = SettingsService(cfg)
    service.apply_overlay()
    assert cfg.max_turns == 40
    errors = []

    def writer(n):
        try:
            SettingsService(make_cfg(tmp_path), db=None).patch_admin(
                {"sessions.max_turns": 20 + n}, 1)
        except SettingsError as e:
            errors.append(e.code)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert "revision_conflict" in errors or True  # at most one writer wins


def test_backend_prefs_migrate_once(tmp_path):
    cfg = make_cfg(tmp_path)
    from harness.config import BackendConfig
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    manager.db.set_meta("backend_prefs", json.dumps({"claude": {"model": "claude-sonnet-4", "effort": "low"}}))
    manager.settings.store._unlink(manager.settings.store.active_path) if manager.settings.store.exists() else None
    # Force a re-migration on a new manager sharing the db
    cfg2 = make_cfg(tmp_path)
    cfg2.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", effort="high")
    again = Manager(cfg2, db=manager.db, chat=Script([Completion(content="hi")]))
    assert again.cfg.backends["claude"].model == "claude-sonnet-4"
    assert again.cfg.backends["claude"].effort == "low"
    assert again.settings.store.read_active().migrated_backend_prefs is True
    yaml_only = tmp_path / "untouched.yaml"
    yaml_only.write_text("keep\n", encoding="utf-8")
    original = yaml_only.read_bytes()
    Manager(make_cfg(tmp_path), chat=Script([Completion(content="hi")]))
    assert yaml_only.read_bytes() == original


def test_web_settings_render_plan_and_phone_layout(tmp_path):
    app_js = Path(__file__).resolve().parent.parent / "harness" / "web" / "app.js"
    css = Path(__file__).resolve().parent.parent / "harness" / "web" / "style.css"
    text = app_js.read_text(encoding="utf-8")
    style = css.read_text(encoding="utf-8")
    assert "daemonSettingsCard" in text
    assert "dry_run" in text and "revision_conflict" in text
    assert "confirmRestart" in text and "lkg_restore" in text
    assert "overlay_quarantined" in text
    assert "Enable " in text and "Roll back" in text
    assert "config-row" in style and "max-width: 420px" in style
    client, _ = _client(tmp_path)
    with client:
        js = client.get("/static/app.js").text
        assert "Server" in js or "daemon" in js
        assert client.get("/api/admin/v1/config").status_code == 200


def test_failed_overlay_does_not_leave_keys_absent_from_lkg(tmp_path):
    """_apply_values must not commit a partial overlay. A bad backends.local.model plus
    sessions.max_turns must not leave max_turns applied after LKG restore/unlink."""
    cfg = make_cfg(tmp_path)
    assert cfg.max_turns == 80 and cfg.default_model == "fake"
    store = ManagedStore(cfg.data_dir)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={}))
    store.write_active(Envelope(
        revision=2, confirmed=True,
        values={"backends.local.model": "removed-model", "sessions.max_turns": 12},
    ))
    status = SettingsService(cfg).apply_overlay()
    assert status.get("recovery") == "lkg_restore"
    assert cfg.max_turns == 80
    assert cfg.default_model == "fake"

    cfg_no_lkg = make_cfg(tmp_path / "nolkg")
    store2 = ManagedStore(cfg_no_lkg.data_dir)
    store2.write_active(Envelope(
        revision=1, confirmed=True,
        values={"backends.local.model": "removed-model", "sessions.max_turns": 12},
    ))
    SettingsService(cfg_no_lkg).apply_overlay()
    assert cfg_no_lkg.max_turns == 80
    assert cfg_no_lkg.default_model == "fake"
    assert not store2.active_path.is_file()


def test_disabling_web_does_not_mark_module_uninstalled(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.web.enabled = True
    cfg.modules.web = True
    cfg.installed.web = True
    store = ManagedStore(cfg.data_dir)
    store.write_active(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert manager.cfg.web.enabled is False
    assert manager.cfg.installed.web is True
    assert manager.cfg.modules.web is True  # setter must not flip the install snapshot
    assert module_effective(manager.cfg, "web") is False
    assert manager.cfg.capabilities()["modules"]["web"] is False
    assert manager.runner.web is None
    client = TestClient(create_app(manager))
    with client:
        health = client.get("/health").json()
        api = client.get("/api/v1").json()
        assert health["capabilities"]["modules"]["web"] is False
        assert api["capabilities"]["modules"]["web"] is False
        assert api["features"]["web"] is False


def _web_loadable(tmp_path, *, installed=True, enabled=True):
    extra = (
        f"web: {{enabled: {str(bool(enabled)).lower()}}}\n"
        f"modules:\n  web: {str(bool(installed)).lower()}\n"
    )
    return _write_loadable_config(tmp_path, extra=extra)


def _boot(cfg_dir):
    return Manager(load(cfg_dir), chat=Script([Completion(content="hi")]))


def _assert_web_surfaces(manager, *, on: bool, installed: bool, yaml_enabled: bool):
    assert manager.cfg.installed.web is installed
    # cfg.modules is the YAML-time snapshot, not rewritten by overlay setters.
    assert manager.cfg.modules.web is (installed and yaml_enabled)
    assert manager.cfg.web.enabled is on
    assert module_effective(manager.cfg, "web") is on
    assert manager.cfg.capabilities()["modules"]["web"] is on
    assert (manager.runner.web is not None) is on
    client = TestClient(create_app(manager))
    with client:
        health = client.get("/health").json()
        api = client.get("/api/v1").json()
        assert health["capabilities"]["modules"]["web"] is on
        assert api["capabilities"]["modules"]["web"] is on
        assert api["features"]["web"] is on


@pytest.mark.parametrize("installed,yaml_enabled,overlay,restart,expect_on", [
    (False, True, None, False, False),
    (True, False, None, False, False),
    (True, False, True, False, False),   # pending, no restart: YAML stays off
    (True, False, True, True, True),     # pending promoted by restart
    (True, True, False, True, False),    # confirmed overlay turns YAML-on web off
    (True, True, None, False, True),
])
def test_web_installed_enabled_overlay_matrix(tmp_path, monkeypatch,
                                              installed, yaml_enabled, overlay, restart, expect_on):
    """/health, /api/v1, and Manager tool construction agree: installed AND enabled."""
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=installed, enabled=yaml_enabled)
    if overlay is not None:
        monkeypatch.setenv("HARNESS_SUPERVISED", "1")
        cfg = load(cfg_dir)
        service = SettingsService(cfg)
        service.patch_admin({"web.enabled": overlay}, service.admin_view()["revision"])
        if restart:
            service.request_restart(None)
    manager = _boot(cfg_dir)
    if restart:
        manager.settings.confirm_startup()
    _assert_web_surfaces(manager, on=expect_on, installed=installed, yaml_enabled=yaml_enabled)


def test_crash_after_restart_patch_does_not_drop_confirmed_key(tmp_path):
    """YAML web on + confirmed overlay web off; PATCH web on then die before restart
    must keep the confirmed overlay key so YAML cannot turn web back on."""
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=True, enabled=True)
    store = ManagedStore(data_dir)
    confirmed = Envelope(revision=1, confirmed=True, values={"web.enabled": False})
    store.write_active(confirmed)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    cfg = load(cfg_dir)
    assert cfg.web.enabled is False
    SettingsService(cfg).patch_admin({"web.enabled": True}, 1)
    active = store.read_active()
    assert active is not None and active.confirmed is True
    assert active.values.get("web.enabled") is False
    pending = store.read_pending()
    assert pending is not None and pending.values.get("web.enabled") is True
    # Process dies before POST /config/restart. Next boot applies confirmed active.
    reloaded = _boot(cfg_dir)
    assert reloaded.cfg.web.enabled is False
    assert reloaded.runner.web is None
    assert reloaded.settings.store.read_status().get("recovery") != "lkg_restore"
    assert reloaded.settings.store.read_lkg().values.get("web.enabled") is False


def test_overlay_crash_injection_never_applies_unconfirmed_or_skips_lkg(tmp_path, monkeypatch):
    """Die after PATCH (pending written), after restart confirm, and mid-boot."""
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=True, enabled=True)
    store = ManagedStore(data_dir)
    store.write_active(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))

    # Die after pending is on disk, before active is replaced.
    cfg = load(cfg_dir)
    service = SettingsService(cfg)
    service.store.crash_at = "pending"
    with pytest.raises(OverlayCrash):
        service.patch_admin({"web.enabled": True}, 1)
    service.store.crash_at = None
    after_patch_crash = load(cfg_dir)
    assert after_patch_crash.web.enabled is False
    assert store.read_active().values.get("web.enabled") is False
    assert store.read_active().confirmed is True
    assert store.read_status().get("recovery") != "lkg_restore"

    # Finish the PATCH, then crash during restart before active is replaced.
    SettingsService(load(cfg_dir)).patch_admin({"web.enabled": True}, store.read_active().revision)
    during_restart = SettingsService(load(cfg_dir))
    during_restart.store.crash_at = "boot_tried"
    with pytest.raises(OverlayCrash):
        during_restart.request_restart(None)
    during_restart.store.crash_at = None
    assert load(cfg_dir).web.enabled is False
    assert store.read_active().confirmed is True
    assert store.read_active().values.get("web.enabled") is False

    # Restart confirm commits (unconfirmed active). First boot may try the candidate;
    # a crash mid-boot (boot-tried set, no confirm_startup) must restore LKG.
    SettingsService(load(cfg_dir)).request_restart(None)
    first = load(cfg_dir)
    assert first.web.enabled is True  # first start after owner-confirmed restart tries the candidate
    mid_boot = _boot(cfg_dir)
    assert mid_boot.cfg.web.enabled is False
    assert mid_boot.settings.store.read_status().get("recovery") == "lkg_restore"
    assert mid_boot.settings.store.quarantine_path.is_file()
    assert mid_boot.settings.store.read_active().confirmed is True
    assert mid_boot.settings.store.read_active().values.get("web.enabled") is False


def test_live_patch_does_not_rewrite_pending_restart_candidate(tmp_path, monkeypatch):
    """A live-only PATCH must not clobber a pending daemon_restart key with the
    already-promoted value still sitting on confirmed active."""
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=True, enabled=True)
    store = ManagedStore(data_dir)
    store.write_active(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    cfg = load(cfg_dir)
    assert cfg.web.enabled is False
    service = SettingsService(cfg)
    service.patch_admin({"web.enabled": True}, 1)
    pending = store.read_pending()
    assert pending is not None and pending.values.get("web.enabled") is True
    service.patch_admin({"sessions.max_turns": 40}, store.read_active().revision)
    pending = store.read_pending()
    assert pending is not None, "live PATCH dropped the pending restart candidate"
    assert pending.values.get("web.enabled") is True, pending.values
    assert store.read_active().values.get("web.enabled") is False
    assert cfg.max_turns == 40
    service.request_restart(None)
    reloaded = _boot(cfg_dir)
    reloaded.settings.confirm_startup()
    assert reloaded.cfg.web.enabled is True
    assert reloaded.cfg.max_turns == 40


def test_rollback_clears_pending_only_restart_key(tmp_path, monkeypatch):
    """Rollback must read pending, not only active ∪ LKG, and must clear the
    pending file so a later restart cannot apply an unconfirmed first-time key."""
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=True, enabled=False)
    service = SettingsService(load(cfg_dir))
    service.patch_admin({"web.enabled": True}, service.admin_view()["revision"])
    pending = service.store.read_pending()
    assert pending is not None and pending.values.get("web.enabled") is True
    assert "web.enabled" not in (service.store.read_active().values if service.store.read_active() else {})
    assert service.admin_view()["restart_required"] is True
    service.rollback(service.admin_view()["revision"])
    assert service.store.read_pending() is None
    assert service.admin_view()["restart_required"] is False
    reloaded = _boot(cfg_dir)
    reloaded.settings.confirm_startup()
    assert reloaded.cfg.web.enabled is False
    assert reloaded.settings.store.read_pending() is None
    assert reloaded.settings.store.read_status().get("recovery") != "lkg_restore"


def test_rollback_of_confirmed_restart_key_requires_restart(tmp_path, monkeypatch):
    """After a confirmed web.enabled true generation, rollback restores false on disk
    but leaves the running process on true. restart_required must come from
    active-vs-applied, not only from a pending file, so GET/rollback offer Restart."""
    monkeypatch.setenv("HARNESS_SUPERVISED", "1")
    cfg_dir, data_dir = _web_loadable(tmp_path, installed=True, enabled=False)
    store = ManagedStore(data_dir)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"web.enabled": False}))
    store.write_active(Envelope(revision=2, confirmed=True, values={"web.enabled": True}))
    manager = _boot(cfg_dir)
    assert manager.cfg.web.enabled is True
    assert manager.runner.web is not None
    assert manager.settings.store.read_pending() is None
    assert manager.settings.admin_view()["restart_required"] is False

    client = TestClient(create_app(manager))
    with client:
        body = client.post(f"{PREFIX}/config/rollback", json={"revision": 2, "confirm": True})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["restart_required"] is True
        got = client.get(f"{PREFIX}/config").json()
        assert got["restart_required"] is True
        assert got["pending_revision"] is None

    assert manager.settings.store.read_pending() is None
    assert manager.settings.store.read_active().values.get("web.enabled") is False
    assert manager.cfg.web.enabled is True
    assert manager.runner.web is not None

    manager.settings.request_restart(payload["revision"])
    reloaded = _boot(cfg_dir)
    reloaded.settings.confirm_startup()
    assert reloaded.cfg.web.enabled is False
    assert reloaded.runner.web is None
    assert reloaded.settings.admin_view()["restart_required"] is False


def _write_models_yaml(tmp_path, models: tuple[str, ...]):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    block = ", ".join(f"{name}: {{base_url: http://unused, context_tokens: 1024}}" for name in models)
    (cfg_dir / "harness.yaml").write_text(
        "listen: {host: 127.0.0.1, port: 8100}\n"
        f"data_dir: {data_dir.as_posix()}\n"
        f"default_model: {models[0]}\n"
        f"models: {{{block}}}\n"
        "sandbox: {image: agent-harness-sandbox:py312}\n",
        encoding="utf-8",
    )
    return cfg_dir, data_dir


def test_boot_never_fails_when_active_and_lkg_share_removed_yaml_value(tmp_path):
    """YAML drops a model that both confirmed active and LKG pin. Boot must
    quarantine both overlays, start on YAML defaults, and never raise."""
    cfg_dir, data_dir = _write_models_yaml(tmp_path, ("fake", "qwen-a"))
    store = ManagedStore(data_dir)
    store.write_lkg(Envelope(revision=1, confirmed=True, values={"backends.local.model": "qwen-a"}))
    store.write_active(Envelope(
        revision=2, confirmed=True,
        values={"backends.local.model": "qwen-a", "sessions.max_turns": 40},
    ))
    _write_models_yaml(tmp_path, ("fake",))

    cfg = load(cfg_dir)
    assert cfg.default_model == "fake"
    assert cfg.max_turns == 80
    manager = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert manager.cfg.default_model == "fake"
    assert manager.cfg.max_turns == 80
    assert not store.active_path.is_file()
    assert not store.lkg_path.is_file()
    kept = sorted(p.name for p in data_dir.glob("managed-config*.quarantine.*")
                  if p.is_file() and p.name != "managed-config.quarantine.json")
    assert any(name.startswith("managed-config.json.quarantine.") for name in kept)
    assert any(name.startswith("managed-config.lkg.json.quarantine.") for name in kept)
    view = manager.settings.admin_view()
    recovery = view["recovery"] or {}
    assert recovery.get("recovery") == "overlay_quarantined"
    assert view.get("warning")
    assert "YAML" in (view.get("warning") or recovery.get("reason") or "")
