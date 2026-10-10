"""Synthetic Hub inventory, local strict manifests and module presence (#520)."""
from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import make_dataclass
from types import SimpleNamespace
import json
import socket
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from harness import cli, config, modules
from harness.api import create_app
from harness.manager import Manager
from harness.settings_keys import build_registry
from harness_modules.hub import MODULE
from harness_modules.hub.entries import EXAMPLE, EntriesError, initialize, load
from harness_modules.hub.service import connection_state
from sdk.harness_client import ContractError, Harness
from test_daemon import make_cfg

PATH = "/api/admin/v1/hub"
CATALOG = "ahpub.dflippojr.agent-harness-web"


def make(tmp_path, packages=None):
    cfg = make_cfg(tmp_path)
    cfg.config_dir = tmp_path / "config"
    cfg.module_packages = packages
    return Manager(cfg)


def document():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def save(tmp_path, doc):
    path = tmp_path / "entries.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.mark.parametrize("mutation, named", [
    (lambda d: d["entries"][0].update(unexpected=True), "additionalProperties"),
    (lambda d: d["entries"][0].pop("review"), "required"),
    (lambda d: d["entries"][0]["app"].update(app_id="not-an-id"), "app.app_id"),
    (lambda d: d["entries"][0]["app"].update(browser_origins=["http://example.invalid"]), "browser_origins"),
    (lambda d: d["entries"][0]["app"]["scopes"][0].update(scope="admin"), "scopes.0.scope"),
    (lambda d: d["entries"][0]["app"]["scopes"][0].update(scope="unknown"), "scopes.0.scope"),
    (lambda d: d["entries"].append(copy.deepcopy(d["entries"][0])), "duplicate id"),
    (lambda d: d.update(unexpected=True), "entries array"),
])
def test_entries_fail_closed_with_named_errors(tmp_path, mutation, named):
    doc = document()
    mutation(doc)
    with pytest.raises(EntriesError, match=named):
        load(save(tmp_path, doc))


def test_entries_validate_offline_and_init_refuses_overwrite(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("entries must never use the network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    assert len(load(EXAMPLE)) == 2
    assert load(tmp_path / "missing") == []
    path = initialize(tmp_path / "cfg")
    original = path.read_bytes()
    with pytest.raises(EntriesError, match="refusing to overwrite"):
        initialize(path.parent)
    assert path.read_bytes() == original
    with pytest.raises(EntriesError, match="unreadable"):
        load(tmp_path)
    for content in (b"{", b"\xff"):
        path.write_bytes(content)
        with pytest.raises(EntriesError):
            load(path)


def test_local_cli_needs_no_client_config_or_daemon(tmp_path, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise AssertionError("offline commands do not configure clients or call the daemon")
    monkeypatch.setattr(cli, "configure", denied)
    monkeypatch.setattr(cli, "api", denied)
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path))
    for argv, code in ((["hub", "entries", "init"], 0), (["hub", "entries", "init"], 1),
                       (["hub", "entries", "validate"], 0),
                       (["hub", "entries", "validate", str(tmp_path / "hub.entries.json")], 0)):
        monkeypatch.setattr(cli.sys, "argv", ["harness", *argv])
        assert cli.main() == code
    assert "refusing to overwrite" in capsys.readouterr().out
    args = cli._build_parser().parse_args(["hub", "status"])
    assert cli.admin_request(args) == ("GET", "/hub", {})
    args = cli._build_parser().parse_args(["hub", "claim-status"])
    assert cli.admin_request(args) == ("GET", "/hub-claim", {})


@pytest.mark.parametrize("used, expected", [(None, "never_used"), (60, "active"), (900, "active"),
    (901, "idle"), (1200, "idle"), (3 * 86400, "idle"), (7 * 86400, "idle"),
    (7 * 86400 + 1, "stale"), (30 * 86400, "stale")])
def test_state_thresholds(used, expected):
    now = 10_000_000
    row = {"last_used_at": now - used if used is not None else None}
    assert connection_state(row, now) == expected
    row["revoked_at"] = now
    assert connection_state(row, now) == "revoked"
    row["erase_after"] = now + 10
    assert connection_state(row, now) == "erasure_pending"
    row["erased_at"] = now
    assert connection_state(row, now) == "revoked"


def test_inventory_states_errors_metrics_and_secret_exclusion(tmp_path, monkeypatch, caplog):
    m = make(tmp_path, ["harness_modules.hub"])
    now = 10_000_000
    monkeypatch.setattr("harness_modules.hub.service.time.time", lambda: now)
    secrets = ("synthetic-token-value", "synthetic-hash-value", "synthetic-pairing-code")
    rows = []
    for index, age in enumerate((None, 60, 1200, 3 * 86400, 30 * 86400, 60, 60, 60, 60)):
        rows.append({"id": f"a-{index}", "name": "synthetic", "kind": "app", "scopes": "sessions",
                     "origins": [], "created_at": now - 100, "last_used_at": now - age if age else None,
                     "token": secrets[0], "hash": secrets[1], "pairing_code": secrets[2],
                     "store": {"sessions": {"done": 3}, "usage": {"turns": 4}, "errors": 1,
                               "last_error": "failed", "last_error_at": None, "hash": secrets[1]}})
    rows[5]["revoked_at"] = now - 10
    rows[6].update(revoked_at=now - 10, erase_after=now + 100)
    rows[7]["store"]["last_error_at"] = now - 2 * 3600
    rows[8]["store"]["last_error_at"] = now - 2 * 86400
    monkeypatch.setattr(m.db, "list_api_keys", lambda: rows)
    with TestClient(create_app(m)) as client:
        result = client.get(PATH)
        assert result.status_code == 200, result.text
        inventory = result.json()
        assert [row["state"] for row in inventory["apps"]] == [
            "never_used", "active", "idle", "idle", "stale", "revoked", "erasure_pending", "active", "active"]
        assert [row["errors"] for row in inventory["apps"]] == [False] * 7 + [True, False]
        assert all(row["token_age_seconds"] == 100 for row in inventory["apps"])
        assert inventory["apps"][0]["store"]["sessions"] == {"done": 3}
        assert inventory["apps"][0]["store"]["usage"] == {"turns": 4}
        metrics = client.get("/metrics").text
        assert 'harness_hub_apps{state="active"} 3' in metrics
        assert 'harness_hub_apps{state="idle"} 2' in metrics
        assert "harness_hub_app_errors 1" in metrics
        joined = result.text + metrics + caplog.text
        assert all(secret not in joined for secret in secrets)


def test_entries_match_ids_pending_and_do_not_open_app_stores(tmp_path, monkeypatch):
    m = make(tmp_path, ["harness_modules.hub"])
    initialize(m.cfg.config_dir)
    doc = document()
    doc["entries"][0]["app"]["browser_origins"] = ["https://web.example.invalid"]
    (m.cfg.config_dir / "hub.entries.json").write_text(json.dumps(doc), encoding="utf-8")
    key, token = m.db.create_api_key("display name intentionally different", "sessions", kind="app",
                                    catalog_app_id=CATALOG, origins=["https://web.example.invalid"])
    unmatched, _ = m.db.create_api_key("Agent Harness Web", "sessions", kind="app")
    now = time.time()
    def add_requests():
        # Beyond the ordinary list's newest-100 limit, with secrets the inventory must not project.
        for index in range(105):
            m.db.main.insert_pairing_request({"id": f"pr-{index}", "kind": "app", "name": "request",
                "scopes": "sessions", "catalog_app_id": CATALOG if index == 0 else "ahpub.example.other",
                "origin": "https://web.example.invalid", "source": "", "challenge_hash": "synthetic-challenge", "match_code": "1234",
                "state": "claimed", "armed": 1, "created_at": now + index, "expires_at": now + 500,
                "approved_at": None, "finished_at": None, "key_id": ""})
    m.db.main.write(add_requests)
    def denied(*args, **kwargs):
        raise AssertionError("inventory must not open/read app stores or fetch entry URLs")
    monkeypatch.setattr(m.db, "_acquire", denied)
    with TestClient(create_app(m)) as client:
        monkeypatch.setattr(socket.socket, "connect", denied)
        result = client.get(PATH)
        assert result.status_code == 200, result.text
        entry, other = result.json()["entries"]
        assert entry["paired"] == [key["id"]] and unmatched["id"] not in entry["paired"]
        assert entry["state"] == "paired" and entry["pending_pairing"] is True
        assert other["state"] == "not_paired" and other["pending_pairing"] is False
        assert entry["verified"] is False and other["verified"] is False
        assert token not in result.text and "synthetic-challenge" not in result.text and "1234" not in result.text
        m.db.main.write(lambda: m.db.main.conn.execute("UPDATE pairing_requests SET expires_at = 0"))
        assert client.get(PATH).json()["entries"][0]["pending_pairing"] is False


def test_presence_disabled_absent_and_owner_auth(tmp_path):
    m = make(tmp_path)
    registry = build_registry(m.cfg)
    assert "hub.enabled" in registry.specs
    setting = registry.get("hub.enabled")
    setting.setter(m.cfg, False)
    assert setting.getter(m.cfg) is False
    with TestClient(create_app(m)) as client:
        assert client.get(PATH).status_code == 400
        assert "disabled" in client.get(PATH).text
        setting.setter(m.cfg, True)
        response = client.get(PATH)
        assert response.status_code == 200
        assert ("GET", PATH) in {(r["method"], r["path"]) for r in client.get("/api/admin/v1").json()["operations"]}
        owner, owner_token = m.db.create_api_key("owner", "admin", kind="owner")
        hub, hub_token = m.db.create_api_key("hub", "admin", kind="owner")
        app, app_token = m.db.create_api_key("app", "sessions", kind="app")
        device, device_token = m.db.create_api_key("device", "inference", kind="device")
        m.db.main.write(lambda: m.db.main.conn.execute("UPDATE api_keys SET role = 'hub' WHERE id = ?", (hub["id"],)))
        client.local_owner = False
        assert client.get(PATH).status_code == 401
        for token in (owner_token, hub_token):
            assert client.get(PATH, headers={"Authorization": f"Bearer {token}"}).status_code == 200
            from test_key_activity import drain_activity
            drain_activity(client, m)
            assert m.db.api_key_by_secret(token)["last_used_at"] is not None
        for token in (app_token, device_token):
            assert client.get(PATH, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    absent = make(tmp_path / "absent", [])
    assert "hub.enabled" not in build_registry(absent.cfg).specs
    with TestClient(create_app(absent)) as client:
        assert client.get(PATH).status_code == 404
        assert "harness_hub_" not in client.get("/metrics").text
        assert client.get("/api/admin/v1/keys").status_code == 200
        assert client.get("/api/admin/v1/hub-claim").status_code == 200


def test_optional_status_failure_and_first_implementers(tmp_path, monkeypatch, caplog):
    m = make(tmp_path)
    m.cfg.backup.enabled = True
    m.cfg.notify.enabled = True
    m.modules.get("backup").service.last_backup = {"ok_at": 123, "bytes": 456, "secret": "do-not-return"}
    rt = modules.ModuleRuntime(m, MODULE)
    assert rt.status() is None
    def failure():
        raise RuntimeError("synthetic-secret-in-hook-error")
    monkeypatch.setattr(m.modules.get("homelab"), "status", failure)
    with TestClient(create_app(m)) as client:
        rows = {row["name"]: row for row in client.get(PATH).json()["modules"]}
        assert rows["homelab"]["status"] == {"state": "error"}
        assert "status" not in rows["skills"]
        assert rows["runners"]["status"] == {"online_count": 0}
        assert rows["backup"]["status"] == {"last_success_at": 123, "bytes": 456}
        assert rows["notifications"]["status"] == {"enabled": True, "queued": 0}
        assert rows["local_model"]["status"] == {"state": "ready", "loaded": True, "warming": False}
        assert "synthetic-secret-in-hook-error" not in caplog.text
        assert "do-not-return" not in json.dumps(rows)


def test_doctor_and_invalid_entries_api(tmp_path):
    from types import SimpleNamespace
    m = make(tmp_path, ["harness_modules.hub"])
    m.cfg.config_dir.mkdir()
    (m.cfg.config_dir / "hub.entries.json").write_text("{}", encoding="utf-8")
    results = []
    report = SimpleNamespace(ok=lambda *args: results.append(("ok", args)),
                             fail=lambda *args: results.append(("fail", args)))
    MODULE.doctor(report, m.cfg)
    assert results[0][0] == "fail"
    with TestClient(create_app(m)) as client:
        assert client.get(PATH).status_code == 400


def test_config_yaml_and_service_opt_in(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    path = cfg_dir / "harness.yaml"
    path.write_text("hub: {enabled: false}\nmodels: {fake: {base_url: http://unused}}\n", encoding="utf-8")
    cfg = config.load(cfg_dir, tmp_path / "data")
    assert cfg.installed.hub and not cfg.hub.enabled and not cfg.modules.hub
    path.write_text("profile: service\nmodules: {hub: true}\nbackends: {codex: {enabled: true}}\n", encoding="utf-8")
    cfg = config.load(cfg_dir, tmp_path / "data")
    assert cfg.installed.hub and cfg.hub.enabled and cfg.capabilities()["modules"]["hub"]


def test_sdk_hub_inventory_and_openapi(tmp_path):
    m = make(tmp_path)
    with TestClient(create_app(m)) as client:
        schema = client.get("/openapi.json").json()
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"modules": [], "apps": [], "entries": []})
    sdk = Harness("http://unused.invalid", token="synthetic-owner")
    sdk.client.close()
    sdk.client = httpx.Client(base_url="http://unused.invalid", transport=httpx.MockTransport(handler))
    try:
        assert sdk.hub_status() == {"modules": [], "apps": [], "entries": []}
        assert seen[0].url.path == PATH
        sdk.validate_openapi(schema)
        drifted = copy.deepcopy(schema)
        drifted["components"]["schemas"]["HubInventory"]["properties"].pop("entries")
        with pytest.raises(ContractError, match="entries"):
            sdk.validate_openapi(drifted)
    finally:
        sdk.close()


@pytest.mark.parametrize("app_id", ["a" * 60 + "." + "b" * 60,
    "ha-shop.example", "ho-shop.example", "hp-shop.example", "hk-shop.example", "hrp-shop.example"])
def test_manifest_id_also_obeys_pairing_contract(tmp_path, app_id):
    from jsonschema import Draft202012Validator, FormatChecker
    from harness import catalog_ids
    from harness_modules.hub.entries import SCHEMA
    doc = document()
    doc["entries"][0]["app"]["app_id"] = app_id
    # These still satisfy the unchanged publisher manifest schema, but cannot be paired.
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")),
                         format_checker=FormatChecker()).validate(doc["entries"][0])
    assert not catalog_ids.valid(app_id)
    with pytest.raises(EntriesError, match="app.app_id"):
        load(save(tmp_path, doc))


def test_maximum_pairable_manifest_id(tmp_path):
    from harness import catalog_ids
    app_id = "a" * 60 + "." + "b" * 59
    assert len(app_id) == 120 and catalog_ids.valid(app_id)
    doc = document()
    doc["entries"][0]["app"]["app_id"] = app_id
    assert load(save(tmp_path, doc))[0]["app"]["app_id"] == app_id


@pytest.mark.parametrize("guard_enabled", [False, True])
def test_shared_runtime_detail_is_omitted_for_disabled_switch(tmp_path, guard_enabled):
    cfg = make_cfg(tmp_path)
    cfg.config_dir = tmp_path / "cfg"
    cfg.module_packages = ["harness_modules.local_model", "harness_modules.hub"]
    cfg.gpu_guard.enabled = guard_enabled
    m = Manager(cfg)
    with TestClient(create_app(m)) as client:
        rows = {row["name"]: row for row in client.get(PATH).json()["modules"]}
        assert rows["local_model"]["status"]["loaded"] is True
        if guard_enabled:
            assert rows["gpu_guard"]["state"] == "present"
            assert rows["gpu_guard"]["status"] == rows["local_model"]["status"]
        else:
            assert rows["gpu_guard"] == {"name": "gpu_guard", "state": "switched_off"}


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_entries_reject_non_finite_json_prices(tmp_path, price):
    doc = document()
    doc["entries"][0]["app"]["monetization"]["price_usd"] = "PRICE_MARKER"
    path = tmp_path / "entries.json"
    path.write_text(json.dumps(doc).replace('"PRICE_MARKER"', price), encoding="utf-8")
    with pytest.raises(EntriesError, match="invalid JSON"):
        load(path)


def test_finite_manifest_price_survives_loading(tmp_path):
    doc = document()
    doc["entries"][0]["app"]["monetization"]["price_usd"] = 2.5
    assert load(save(tmp_path, doc))[0]["app"]["monetization"]["price_usd"] == 2.5


@pytest.mark.parametrize("origins, pending_origin, paired, unverified", [
    (["https://web.example.invalid"], "https://web.example.invalid", True, False),
    (["https://attacker.example"], "https://attacker.example", False, False),
    ([], "", False, True),
    (["https://web.example.invalid", "https://attacker.example"], "https://attacker.example", False, False),
])
def test_entry_attribution_requires_manifest_origins(tmp_path, origins, pending_origin, paired, unverified):
    m = make(tmp_path, ["harness_modules.hub"])
    initialize(m.cfg.config_dir)
    doc = document()
    doc["entries"][0]["app"]["browser_origins"] = ["https://web.example.invalid"]
    (m.cfg.config_dir / "hub.entries.json").write_text(json.dumps(doc), encoding="utf-8")
    key, _ = m.db.create_api_key("Agent Harness Web", "sessions", kind="app", origins=origins,
                               catalog_app_id=CATALOG)
    now = time.time()
    m.db.main.write(m.db.main.insert_pairing_request, {
        "id": "synthetic", "kind": "app", "name": "Agent Harness Web", "catalog_app_id": CATALOG,
        "origin": pending_origin, "scopes": "sessions", "source": "synthetic",
        "challenge_hash": "synthetic-challenge", "match_code": "1234",
        "state": "pending", "created_at": now, "expires_at": now + 500})
    with TestClient(create_app(m)) as client:
        response = client.get(PATH)
        assert response.status_code == 200, response.text
        entry = response.json()["entries"][0]
        assert entry["paired"] == ([key["id"]] if paired else [])
        assert entry["state"] == ("paired" if paired else "not_paired")
        assert entry["pending_pairing"] is paired
        assert entry["unverified_origin"] == {
            "paired": [key["id"]] if unverified else [], "pending_pairing": unverified}
        assert key["id"] in [app["id"] for app in response.json()["apps"]]
        m.db.main.write(lambda: m.db.main.conn.execute("UPDATE pairing_requests SET expires_at = 0"))
        expired = client.get(PATH).json()["entries"][0]
        assert expired["pending_pairing"] is False
        assert expired["unverified_origin"]["pending_pairing"] is False


@pytest.mark.parametrize("async_hook", [False, True])
def test_status_timeout_returns_safe_error_for_sync_and_async_hooks(monkeypatch, async_hook):
    from harness_modules.hub import service
    monkeypatch.setattr(service, "STATUS_TIMEOUT", 0.03)
    release = threading.Event()

    def blocked():
        release.wait(2)
        return {"state": "late"}

    async def asleep():
        await asyncio.Event().wait()

    async def exercise():
        try:
            detail = await asyncio.wait_for(service._detail(SimpleNamespace(status=asleep if async_hook else blocked)), 1)
            assert detail == {"state": "error"}
        finally:
            release.set()

    asyncio.run(exercise())


@pytest.mark.parametrize("async_hook", [False, True])
def test_inventory_probes_modules_concurrently(monkeypatch, async_hook):
    from harness_modules.hub import service
    monkeypatch.setattr(service, "STATUS_TIMEOUT", 0.2)
    monkeypatch.setattr(service, "_snapshot", lambda manager, now: ([], []))
    barrier = threading.Barrier(2)

    async def exercise():
        started = set()
        both = asyncio.Event()

        def sync_status():
            barrier.wait(1)
            return {"state": "ready"}

        def runtime(name):
            async def async_status():
                started.add(name)
                if len(started) == 2:
                    both.set()
                await both.wait()
                return {"state": "ready"}
            return SimpleNamespace(module=SimpleNamespace(name=name, switches=(name,)), effective=lambda: True,
                                   status=async_status if async_hook else sync_status)

        installed = make_dataclass("Installed", [("first", bool), ("second", bool)])(True, True)
        cfg = SimpleNamespace(installed=installed, capabilities=lambda: {"modules": {"first": True, "second": True}})
        manager = SimpleNamespace(cfg=cfg, modules=[runtime("first"), runtime("second")])
        inventory = await asyncio.wait_for(service.inventory(manager), 2)
        assert [row["status"] for row in inventory["modules"]] == [{"state": "ready"}] * 2

    asyncio.run(exercise())


def test_status_output_is_bounded_json_scalars_only():
    from harness_modules.hub import service
    payload = {"state": "x" * 1000, "config": {"secret": "synthetic-secret"}, "list": ["synthetic-secret"],
               "count": 12, "enabled": True, "time": 1.5, "missing": None, "invalid": float("nan"),
               "extra": "must-not-appear"}
    result = asyncio.run(service._detail(SimpleNamespace(status=lambda: payload)))
    assert result == {"state": "x" * 200, "count": 12, "enabled": True, "time": 1.5, "missing": None}
    assert "synthetic-secret" not in json.dumps(result, allow_nan=False)
    assert service._bounded_status({str(i): i for i in range(100)}) == {str(i): i for i in range(8)}
    assert service._bounded_status({"x" * 201: "bad", "huge": 1 << 10000, "inf": float("inf"), 123: "bad"}) is None
    assert service._bounded_status(["synthetic-secret"]) is None
