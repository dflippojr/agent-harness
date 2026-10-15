"""Requests that reach the loopback listener without a Tailscale identity must carry the local owner token."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from harness import cli, local_owner
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, make_cfg

LOGIN = "me@example.com"
OTHER = "someone@example.com"


@pytest.fixture
def harness(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [LOGIN]
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    client = TestClient(create_app(m))
    client.local_owner = False  # this client sends only the headers each test gives it
    with client:
        yield client, m


def local(m) -> dict:
    return {local_owner.HEADER: m.local_owner_token}


def test_the_token_is_created_once_and_kept_across_restarts(tmp_path):
    first = local_owner.ensure_token(tmp_path)
    assert len(first) >= 32
    assert local_owner.ensure_token(tmp_path) == first
    assert local_owner.read_token(tmp_path) == first
    if os.name != "nt":
        assert stat.S_IMODE(local_owner.token_path(tmp_path).stat().st_mode) == 0o600


def test_health_and_metrics_answer_without_a_token(harness):
    client, _ = harness
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_an_empty_identity_header_counts_as_no_identity(harness):
    client, m = harness
    assert client.get("/me", headers={"Tailscale-User-Login": ""}).status_code == 401
    assert client.get("/me", headers={"Tailscale-User-Login": "", **local(m)}).json()["role"] == "owner"


def test_a_browser_preflight_needs_no_credential_and_the_request_still_does(harness):
    client, m = harness
    origin = "https://control.example"
    owner = client.post("/keys", json={"name": "o", "kind": "owner", "scopes": ["admin"], "origins": [origin]},
                        headers=local(m)).json()
    preflight = client.options("/api/admin/v1/keys", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})
    assert preflight.status_code == 204
    assert client.get("/api/admin/v1/keys", headers={"Origin": origin}).status_code == 401
    assert client.get("/api/admin/v1/keys", headers={
        "Origin": origin, "Authorization": f"Bearer {owner['key']}"}).status_code == 200


def test_cli_sends_the_local_token_only_to_a_daemon_on_this_machine(tmp_path, monkeypatch):
    data = tmp_path / "data"
    token = local_owner.ensure_token(data)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data))
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("HARNESS_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr(cli, "HARNESS_HOME", tmp_path)
    for server, expected in (("http://127.0.0.1:8100", token), ("http://localhost:8100", token),
                             ("https://tower.example.ts.net", None)):
        monkeypatch.setenv("HARNESS_URL", server)
        cli.configure(tmp_path / "missing.json")
        assert cli._headers().get(local_owner.HEADER) == expected, server
    monkeypatch.setenv("HARNESS_URL", "http://127.0.0.1:8100")
    monkeypatch.setenv("HARNESS_LOCAL_TOKEN", "from-env")
    cli.configure(tmp_path / "missing.json")
    assert cli._headers()[local_owner.HEADER] == "from-env"
    monkeypatch.setenv("HARNESS_URL", "https://tower.example.ts.net")
    cli.configure(tmp_path / "missing.json")
    assert local_owner.HEADER not in cli._headers()
    assert cli.LOCAL_TOKEN_HEADER == local_owner.HEADER
