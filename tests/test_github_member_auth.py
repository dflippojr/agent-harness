"""Issue #63: isolated household-member GitHub authentication, with fake GCM, store, helper, Git, and GitHub."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harness import github_auth as ga
from harness.admin import PREFIX
from harness.clone import isolated_clone_env
from harness.github_auth import GitHubAuthError, canonical_github_url

from github_fakes import FakeGitHub, wait_for
from test_household import ALICE, BOB, OWNER, H, create_member, household

CODE = "WDJB-MJHT"
ME = "/api/v1/me/github-connection"
ALLOWED_STATUS_KEYS = {"status", "deadline", "seconds_left", "last_used_at", "error", "reason", "message",
                       "scopes_note", "prompt"}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    if ga.platform_key() == "linux":
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    # host secrets and Git controls that must never reach the broker's children
    for key, value in {"GH_TOKEN": "gh-host-secret", "GITHUB_TOKEN": "github-host-secret",
                       "GIT_ASKPASS": "/bin/askpass", "SSH_AUTH_SOCK": "/tmp/agent", "HTTPS_PROXY": "http://p:1",
                       "GIT_TRACE": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url.x.insteadOf",
                       "GIT_CONFIG_VALUE_0": "https://github.com/", "GCM_NAMESPACE": "git",
                       "GCM_TRACE": "1"}.items():
        monkeypatch.setenv(key, value)
    return FakeGitHub(tmp_path / "fakes")


def setup(tmp_path, fake, *, enable=True, members=(ALICE, BOB)):
    client, m = household(tmp_path)
    fake.configure(m.cfg)
    client.__enter__()
    ids = {}
    for login in members:
        ids[login] = create_member(client, login, login.split("@")[0].title())["user_id"]
    if enable:
        r = client.put(f"{PREFIX}/github-member-auth", json={"enabled": True}, headers=H(OWNER))
        assert r.status_code == 200, r.text
        assert r.json()["preflight"]["ok"], r.json()
    return client, m, ids


def connect(client, fake, login, uid, approve=True) -> dict:
    r = client.post(f"{ME}/connect", headers=H(login))
    assert r.status_code == 200, r.text
    body = wait_for(lambda: (lambda b: b if b.get("prompt") else None)(client.get(ME, headers=H(login)).json()))
    assert body and body["status"] == "connecting"
    assert body["prompt"] == {"verification_uri": "https://github.com/login/device", "user_code": CODE}
    if approve:
        fake.approve(uid, login.split("@")[0])
        done = wait_for(lambda: client.get(ME, headers=H(login)).json()["status"] == "connected")
        assert done, client.get(ME, headers=H(login)).json()
    return body


def close(client):
    client.__exit__(None, None, None)


# --- URL policy ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "", "   ", "http://github.com/o/r", "https://user@github.com/o/r", "https://user:tok@github.com/o/r",
    "https://github.com:443/o/r", "https://github.com:8443/o/r", "https://github.com/o/r?x=1",
    "https://github.com/o/r#readme", "https://www.github.com/o/r", "https://github.com./o/r",
    "https://gitlab.com/o/r", "https://githüb.com/o/r", "https://xn--gthub-cta.com/o/r",
    "https://github.com.evil.example/o/r", "https://evil.example/github.com/o/r", "git@github.com:o/r.git",
    "ssh://git@github.com/o/r.git", "git://github.com/o/r.git", "file:///tmp/r", "C:/repos/r", "/srv/r",
    "../r", "r.bundle", "ext::sh -c touch% /tmp/pwned", "fd::17", "https://github.com/o", "https://github.com/",
    "https://github.com/o/r/tree/main", "https://github.com/o/../r", "https://github.com/o/.r",
    "https://github.com/-o/r", "https://github.com/o/r%2f..", "https://github.com/o/r\nx","https://github.com/o r/x",
    "https://github.com\\o\\r", "HTTPS://github.com/o/r/..", "https://github.com//o/r", "https://github.com/o/r.git/",
])
def test_github_url_refusals(url):
    with pytest.raises(GitHubAuthError) as e:
        canonical_github_url(url)
    assert e.value.code == "policy_rejected"


def test_github_url_is_reparsed_and_rendered():
    assert canonical_github_url("https://github.com/octo-org/hello.world") == "https://github.com/octo-org/hello.world.git"
    assert canonical_github_url(" https://GitHub.com/o/r.git ") == "https://github.com/o/r.git"
    assert ga.display_repo("https://github.com/o/r.git") == "o/r"


def test_redaction_patterns():
    raw = ("password=gho_abcdefghijklmnop1234 ghp_0123456789abcdefghij user code WDJB-MJHT "
           "Authorization: Bearer abc https://u:secret@github.com/o/r")
    out = ga.redact(raw)
    for leaked in ("gho_abcdefghijklmnop1234", "ghp_0123456789abcdefghij", "WDJB-MJHT", "secret@", "Bearer abc"):
        assert leaked not in out


def test_git_failure_classes_are_generic():
    assert ga.classify_git_failure("fatal: Authentication failed for 'x'") == "reconnect_required"
    assert ga.classify_git_failure("fatal: could not read Username: terminal prompts disabled") == "reconnect_required"
    assert ga.classify_git_failure("remote: Repository not found.") == "repository_unavailable"
    assert ga.classify_git_failure("The requested URL returned error: 301") == "repository_unavailable"
    assert ga.classify_git_failure("something odd") == "git_failed"
    for code, message in ga.ERRORS.items():
        assert "fatal" not in message and "stderr" not in message, code


# --- environment, config, and namespace isolation ----------------------------------------------------------

def test_broker_env_is_minimal_and_namespaced(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake, enable=False)
    try:
        a, b = ids[ALICE], ids[BOB]
        env_a, env_b = ga.broker_env(m.cfg, a), ga.broker_env(m.cfg, b)
        for env in (env_a, env_b):
            for leaked in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_AUTH_SOCK", "HTTPS_PROXY", "GIT_TRACE",
                           "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GCM_TRACE", "GCM_GITHUB_HELPER"):
                assert leaked not in env, leaked
            assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
            assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_SYSTEM"] == os.devnull
            assert env["GCM_PROVIDER"] == "github" and env["GCM_GITHUB_AUTHMODES"] == "device"
            assert Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8").startswith("#")
        assert env_a["GCM_NAMESPACE"] == f"agent-harness/v1/{a}" != env_b["GCM_NAMESPACE"]
        assert env_a["HOME"] != env_b["HOME"] and env_a["GIT_CONFIG_GLOBAL"] != env_b["GIT_CONFIG_GLOBAL"]
        data = Path(m.cfg.data_dir).resolve()
        for env in (env_a, env_b):
            home = Path(env["HOME"]).resolve()
            assert data / "github-broker" in home.parents
            assert (data / "users") not in home.parents  # not under a member's storage or workspace root
        args = ga.git_config_args(m.cfg, a)
        pairs = [args[i + 1] for i in range(0, len(args), 2)]
        assert pairs[0] == "credential.helper=" and pairs[1] == f'credential.helper="{fake.gcm.as_posix()}"'
        for expected in ("http.followRedirects=false", "protocol.allow=never", "protocol.https.allow=always",
                         "http.extraHeader=", "http.proxy=", "submodule.recurse=false", "core.askPass=",
                         f"credential.namespace=agent-harness/v1/{a}", "filter.lfs.smudge="):
            assert expected in pairs, expected
        with pytest.raises(GitHubAuthError):
            ga.namespace_for("owner")
        with pytest.raises(GitHubAuthError):
            ga.namespace_for("../x")
    finally:
        close(client)


def test_public_member_clone_env_unchanged_when_feature_enabled(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        env = isolated_clone_env()
        assert "GCM_NAMESPACE" not in env and env["GIT_CONFIG_VALUE_0"] == "" and env["GCM_INTERACTIVE"] == "Never"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    finally:
        close(client)


# --- preflight -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform,store,ok", [
    ("win32", "wincredman", True), ("win32", "dpapi", True), ("win32", "keychain", False),
    ("darwin", "keychain", True), ("darwin", "wincredman", False),
    ("linux", "secretservice", True), ("linux", "gpg", False),  # gpg needs an initialized pass store
    ("win32", "plaintext", False), ("linux", "cache", False), ("darwin", "none", False), ("win32", "", False),
])
def test_store_adapters(tmp_path, fake, monkeypatch, platform, store, ok):
    client, m = household(tmp_path)
    fake.configure(m.cfg, store)
    monkeypatch.setattr(ga, "platform_key", lambda: platform)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    if ok:
        settings = ga.store_settings(m.cfg, "u-alice")
        assert settings["GCM_CREDENTIAL_STORE"] == store
        if store == "dpapi":
            assert "u-alice" in settings["GCM_DPAPI_STORE_PATH"]
        assert ga.run_preflight(m.cfg).ok
    else:
        with pytest.raises(GitHubAuthError) as e:
            ga.store_settings(m.cfg, "u-alice")
        assert e.value.code == "store_unavailable"
        assert ga.run_preflight(m.cfg).error == "store_unavailable"


def test_gpg_and_secret_service_adapters(tmp_path, fake, monkeypatch):
    client, m = household(tmp_path)
    fake.configure(m.cfg, "gpg")
    monkeypatch.setattr(ga, "platform_key", lambda: "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    store = tmp_path / "pass-store"
    store.mkdir()
    m.cfg.github_member_auth.gpg_pass_store_path = str(store)
    with pytest.raises(GitHubAuthError):
        ga.store_settings(m.cfg, "u-a")  # no .gpg-id: not initialized
    (store / ".gpg-id").write_text("KEYID\n", encoding="utf-8")
    monkeypatch.setattr(ga.shutil, "which", lambda name: "/usr/bin/gpg" if name == "gpg" else None)
    settings = ga.store_settings(m.cfg, "u-a")
    assert settings["PASSWORD_STORE_DIR"] == str(store) and settings["GCM_GPG_PATH"] == "/usr/bin/gpg"
    m.cfg.github_member_auth.credential_store = "secretservice"
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    with pytest.raises(GitHubAuthError):
        ga.store_settings(m.cfg, "u-a")  # no session bus: keyring unavailable, no downgrade
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/x")
    assert ga.run_preflight(m.cfg).error == "unsupported_context"


def test_preflight_refuses_unsafe_or_incompatible_gcm(tmp_path, fake, monkeypatch):
    client, m = household(tmp_path)
    fake.configure(m.cfg)
    assert ga.run_preflight(m.cfg).ok
    # member-writable location: anything under data_dir (member storage and workspaces live there)
    inside = Path(m.cfg.data_dir) / "users" / "u-x" / "git-credential-manager.cmd"
    inside.parent.mkdir(parents=True)
    inside.write_text(fake.gcm.read_text(encoding="utf-8"), encoding="utf-8")
    m.cfg.github_member_auth.gcm_path = str(inside)
    assert ga.run_preflight(m.cfg).error == "not_configured"
    m.cfg.github_member_auth.gcm_path = "git-credential-manager"  # not absolute
    assert ga.run_preflight(m.cfg).error == "not_configured"
    m.cfg.github_member_auth.gcm_path = str(fake.gcm) + '"; rm -rf /'
    assert ga.run_preflight(m.cfg).error == "not_configured"
    if os.name != "nt":
        m.cfg.github_member_auth.gcm_path = str(fake.gcm)
        os.chmod(fake.gcm, 0o777)
        assert ga.run_preflight(m.cfg).error == "not_configured"
        os.chmod(fake.gcm, 0o755)
        link = tmp_path / "gcm-link"
        link.symlink_to(fake.gcm)
        m.cfg.github_member_auth.gcm_path = str(link)
        assert ga.run_preflight(m.cfg).error == "not_configured"
    fake.configure(m.cfg)
    fake.mode(version="2.6.1")
    assert ga.run_preflight(m.cfg).error == "incompatible_gcm"
    fake.mode(store_noop=True)
    assert ga.run_preflight(m.cfg).error == "store_unavailable"
    fake.mode(erase_noop=True)
    assert ga.run_preflight(m.cfg).error == "incompatible_gcm"
    assert fake.namespaces() == {ga.PREFLIGHT_NAMESPACE}  # only a dummy, in the preflight namespace
    fake.wipe_store()
    fake.mode(store_broken=True)
    assert ga.run_preflight(m.cfg).error == "store_unavailable"
    fake.mode()
    assert ga.run_preflight(m.cfg).ok
    # preflight uses only its own namespace and leaves nothing behind
    assert fake.store() == {}
    assert {c["env"]["GCM_NAMESPACE"] for c in fake.gcm_calls()} == {ga.PREFLIGHT_NAMESPACE}


def test_feature_default_off_and_owner_controls(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake, enable=False)
    try:
        r = client.get(ME, headers=H(ALICE))
        assert r.status_code == 200 and r.json()["status"] == "disabled"
        assert r.json()["reason"] == "feature_disabled"
        assert client.post(f"{ME}/connect", headers=H(ALICE)).status_code == 409
        view = client.get(f"{PREFIX}/github-member-auth", headers=H(OWNER)).json()
        assert view["configured"] and not view["enabled"]
        assert {row["status"] for row in view["members"]} == {"disconnected"}
        # members cannot use the owner switch; the owner cannot connect as a member
        assert client.put(f"{PREFIX}/github-member-auth", json={"enabled": True}, headers=H(ALICE)).status_code == 403
        assert client.post(f"{ME}/connect", headers=H(OWNER)).status_code == 403
        assert client.get(ME, headers=H(OWNER)).status_code == 403
        m.cfg.github_member_auth.gcm_path = ""
        r = client.put(f"{PREFIX}/github-member-auth", json={"enabled": True}, headers=H(OWNER))
        assert r.status_code == 409
    finally:
        close(client)


# --- connection lifecycle ----------------------------------------------------------------------------------

def test_connect_routes_device_code_only_to_requesting_member(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a = ids[ALICE]
        connect(client, fake, ALICE, a, approve=False)
        # Bob and the owner see nothing of Alice's prompt
        bob = client.get(ME, headers=H(BOB)).json()
        assert bob["status"] == "disconnected" and "prompt" not in bob
        owner = client.get(f"{PREFIX}/github-member-auth", headers=H(OWNER)).json()
        assert CODE not in json.dumps(owner)
        assert {r["user_id"]: r["status"] for r in owner["members"]}[a] == "connecting"
        # a second connect (refresh) resumes the same live attempt rather than starting another
        again = client.post(f"{ME}/connect", headers=H(ALICE)).json()
        assert again["status"] == "connecting" and again["prompt"]["user_code"] == CODE
        assert sum(1 for c in fake.gcm_calls() if c["action"] == "get" and c["env"]["GCM_INTERACTIVE"] == "always") == 1
        status = client.get(ME, headers=H(ALICE)).json()
        assert set(status) <= ALLOWED_STATUS_KEYS
        assert 0 < status["seconds_left"] <= ga.DEVICE_FLOW_SECONDS
        fake.approve(a, "alice-gh")
        assert wait_for(lambda: client.get(ME, headers=H(ALICE)).json()["status"] == "connected")
        final = client.get(ME, headers=H(ALICE)).json()
        assert "prompt" not in final and "alice-gh" not in json.dumps(final)
        assert fake.namespaces() == {f"agent-harness/v1/{a}"}
        connect_calls = [c for c in fake.gcm_calls() if c["env"].get("GCM_INTERACTIVE") == "always"]
        assert all(c["env"]["GCM_GITHUB_AUTHMODES"] == "device" and c["env"]["GCM_PROVIDER"] == "github"
                   for c in connect_calls)
        # every non-connect GCM call was noninteractive and had no UI helper
        others = [c for c in fake.gcm_calls() if c not in connect_calls]
        assert others and all(c["env"]["GCM_INTERACTIVE"] == "never" and "GCM_GITHUB_HELPER" not in c["env"]
                              for c in others)
        assert all("GH_TOKEN" not in c["env"] and "GITHUB_TOKEN" not in c["env"] for c in fake.gcm_calls())
    finally:
        close(client)


def test_connect_cancel_timeout_and_denied(tmp_path, fake, monkeypatch):
    client, m, ids = setup(tmp_path, fake)
    try:
        a, b = ids[ALICE], ids[BOB]
        connect(client, fake, ALICE, a, approve=False)
        r = client.post(f"{ME}/cancel", headers=H(ALICE)).json()
        assert r["status"] == "disconnected" and r["error"] == "connect_cancelled", r
        assert fake.store() == {}
        fake.deny(b)
        client.post(f"{ME}/connect", headers=H(BOB))
        assert wait_for(lambda: client.get(ME, headers=H(BOB)).json()["error"] == "authorization_failed")
        assert client.get(ME, headers=H(BOB)).json()["status"] == "disconnected"
        monkeypatch.setattr(ga, "DEVICE_FLOW_SECONDS", 2)
        client.post(f"{ME}/connect", headers=H(ALICE))
        assert wait_for(lambda: client.get(ME, headers=H(ALICE)).json()["error"] == "connect_timeout", timeout=20)
        assert fake.store() == {}
    finally:
        close(client)


def test_connect_reports_unsupported_context_without_downgrade(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        fake.mode(no_desktop=True)
        client.post(f"{ME}/connect", headers=H(ALICE))
        assert wait_for(lambda: client.get(ME, headers=H(ALICE)).json()["error"] == "unsupported_context")
    finally:
        close(client)


def test_disconnect_reset_and_isolation_matrix(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a, b = ids[ALICE], ids[BOB]
        connect(client, fake, ALICE, a)
        connect(client, fake, BOB, b)
        assert fake.namespaces() == {f"agent-harness/v1/{a}", f"agent-harness/v1/{b}"}
        # Bob's disconnect erases only Bob's namespace
        r = client.delete(ME, headers=H(BOB))
        assert r.status_code == 200 and r.json()["status"] == "disconnected"
        assert fake.namespaces() == {f"agent-harness/v1/{a}"}
        # the owner's reset needs explicit confirmation and erases without using the credential
        assert client.post(f"{PREFIX}/accounts/{a}/github-connection/reset", json={},
                           headers=H(OWNER)).status_code == 400
        before = len(fake.git_calls())
        r = client.post(f"{PREFIX}/accounts/{a}/github-connection/reset", json={"confirm": True}, headers=H(OWNER))
        assert r.status_code == 200
        assert fake.namespaces() == set() and len(fake.git_calls()) == before
        assert client.get(ME, headers=H(ALICE)).json()["status"] == "disconnected"
        # no principal can act on another member's connection: there is no user id in the member routes
        assert client.post(f"{PREFIX}/accounts/{a}/github-connection/reset", json={"confirm": True},
                           headers=H(BOB)).status_code == 403
        # cross-site browser requests are refused for credential actions
        r = client.post(f"{ME}/connect", headers={**H(ALICE), "Origin": "https://evil.example"})
        assert r.status_code in (401, 403)
        r = client.delete(ME, headers={**H(ALICE), "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        audit = json.dumps(m.db.list_audit(500))
        assert "github_erase" in audit and CODE not in audit
    finally:
        close(client)


def test_member_and_feature_disable_stop_attempts_without_erasing(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a, b = ids[ALICE], ids[BOB]
        connect(client, fake, BOB, b)
        connect(client, fake, ALICE, a, approve=False)
        r = client.patch(f"{PREFIX}/accounts/{a}", json={"enabled": False}, headers=H(OWNER))
        assert r.status_code == 200
        assert wait_for(lambda: not m.github_auth._attempt(a).live)
        assert m.github_auth._attempt(a).outcome == "account_disabled"
        assert client.get(ME, headers=H(ALICE)).status_code == 403
        client.put(f"{PREFIX}/github-member-auth", json={"enabled": False}, headers=H(OWNER))
        # disabling the feature blocks new use but does not silently erase Bob's credential
        assert fake.namespaces() == {f"agent-harness/v1/{b}"}
        status = client.get(ME, headers=H(BOB)).json()
        assert status["status"] == "disabled" and status["reason"] == "feature_disabled"
        with pytest.raises(GitHubAuthError) as e:
            m.github_auth.require_connected(b)
        assert e.value.code == "feature_disabled"
    finally:
        close(client)


def test_reconnect_replaces_only_that_members_credential(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a, b = ids[ALICE], ids[BOB]
        connect(client, fake, ALICE, a)
        connect(client, fake, BOB, b)
        bob_before = fake.store()[f"agent-harness/v1/{b}|{m.cfg.github_member_auth.credential_store}|github.com"]
        (fake.gcm_dir / "approvals" / f"agent-harness_v1_{a}").unlink()
        connect(client, fake, ALICE, a)
        store = fake.store()
        assert store[f"agent-harness/v1/{b}|{m.cfg.github_member_auth.credential_store}|github.com"] == bob_before
        assert len(store) == 2
    finally:
        close(client)


def test_restart_reconciles_missing_credentials_and_drops_attempts(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    a, b = ids[ALICE], ids[BOB]
    connect(client, fake, ALICE, a)
    connect(client, fake, BOB, b, approve=False)
    close(client)  # daemon shutdown cancels Bob's live attempt
    assert not m.github_auth._attempt(b).live
    fake.wipe_store()  # e.g. the database was restored from a backup on another machine
    from harness.manager import Manager
    from fastapi.testclient import TestClient
    from harness.api import create_app
    from test_daemon import Script
    m2 = Manager(m.cfg, db=m.db, chat=Script([]))
    client2 = TestClient(create_app(m2))
    with client2:
        assert wait_for(lambda: m.db.get_github_connection(a)["status"] == "reconnect_required")
        assert client2.get(ME, headers=H(ALICE)).json()["status"] == "reconnect_required"
        assert client2.get(ME, headers=H(BOB)).json()["status"] == "disconnected"


# --- credentialed Git ----------------------------------------------------------------------------------

def _project(client, login, name, url, github=True):
    return client.post("/api/v1/projects", json={"name": name, "repo": url, "github": github}, headers=H(login))


def test_private_clone_fetch_push_and_refused_retry_after_revocation(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a = ids[ALICE]
        fake.add_repo("alice-gh/secret", allowed=["alice"])
        url = "https://github.com/alice-gh/secret"
        # not connected yet: refused before any Git runs
        r = _project(client, ALICE, "secret", url)
        assert r.status_code == 409 and "Connect your GitHub account" in r.json()["detail"]
        assert fake.git_calls() == []
        connect(client, fake, ALICE, a)
        r = _project(client, ALICE, "secret", url)
        assert r.status_code == 201, r.text
        row = m.db.get_member_project(a, "secret")
        assert row["source_auth"] == "github" and row["source_url"] == "https://github.com/alice-gh/secret.git"
        managed = Path(row["repo"])
        assert (managed / "README.md").read_text(encoding="utf-8") == "private\n"
        assert fake.auth_log()[-1] == {"sub": "clone", "repo": "alice-gh/secret", "login": "alice"}
        clone_call = [c for c in fake.git_calls() if "clone" in c["argv"]][-1]
        assert "GH_TOKEN" not in clone_call["env"] and "GIT_CONFIG_COUNT" not in clone_call["env"]
        assert clone_call["env"]["GCM_NAMESPACE"] == f"agent-harness/v1/{a}"
        assert not any(t in json.dumps(clone_call) for t in fake.issued())
        # the managed repo's config holds no token and points at the canonical origin
        cfg_text = (managed / ".git" / "config").read_text(encoding="utf-8")
        assert "https://github.com/alice-gh/secret.git" in cfg_text
        assert not any(t in cfg_text for t in fake.issued())
        # Bob is not connected and cannot use Alice's connection for the same repository
        r = _project(client, BOB, "secret", url)
        assert r.status_code == 409
        # fetch: upstream moves, the broker fetches and fast-forwards the managed copy
        fake.commit_upstream("alice-gh/secret", "NEW.md", "new\n")
        from harness.storage import repos_dir
        assert m.github_auth.fetch(a, row["source_url"], managed, repos_dir(m.cfg, a)) == ""
        assert (managed / "NEW.md").exists()
        # push: a session branch in the managed repo goes to the stored origin with the exact refspec
        _git(managed, "checkout", "-q", "-b", "agent/s1")
        (managed / "work.txt").write_text("work\n", encoding="utf-8")
        _git(managed, "add", "work.txt")
        _git(managed, "-c", "user.name=Agent", "-c", "user.email=a@x", "commit", "-qm", "work")
        _git(managed, "checkout", "-q", "main")
        msg = m.github_auth.push(a, row["source_url"], managed, repos_dir(m.cfg, a), "agent/s1")
        assert msg == "pushed agent/s1 to alice-gh/secret"
        bare = fake.server["repos"]["alice-gh/secret"]["path"]
        assert _git(Path(bare), "rev-parse", "--verify", "refs/heads/agent/s1").strip()
        push_call = [c for c in fake.git_calls() if "push" in c["argv"]][-1]
        assert push_call["argv"][-1] == "refs/heads/agent/s1:refs/heads/agent/s1"
        assert "--force" not in push_call["argv"] and "--tags" not in push_call["argv"]
        for bad in ("main", "agent/../main", "refs/heads/x", "+agent/s1", "agent/s1:main"):
            with pytest.raises(GitHubAuthError):
                m.github_auth.push(a, row["source_url"], managed, repos_dir(m.cfg, a), bad)
        assert m.db.get_github_connection(a)["last_used_at"]
        # GitHub-side revocation: the next push fails closed and the credential is erased
        fake.revoke_all()
        with pytest.raises(GitHubAuthError) as e:
            m.github_auth.push(a, row["source_url"], managed, repos_dir(m.cfg, a), "agent/s1")
        assert e.value.code == "reconnect_required"
        assert fake.namespaces() == set()
        assert client.get(ME, headers=H(ALICE)).json()["status"] == "reconnect_required"
        calls = len(fake.git_calls())
        with pytest.raises(GitHubAuthError) as e:
            m.github_auth.push(a, row["source_url"], managed, repos_dir(m.cfg, a), "agent/s1")
        assert e.value.code == "reconnect_required" and len(fake.git_calls()) == calls  # no Git, no prompt
        _assert_no_secrets(m, fake, tmp_path)
    finally:
        close(client)


def test_unauthorized_repo_redirect_and_tampered_config(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a = ids[ALICE]
        connect(client, fake, ALICE, a)
        fake.add_repo("someone/else", allowed=["mallory"])
        r = _project(client, ALICE, "else", "https://github.com/someone/else")
        assert r.status_code == 409 and r.json()["detail"] == ga.ERRORS["repository_unavailable"]
        assert not (Path(m.cfg.data_dir) / "users" / a / "repos" / "else").exists()
        fake.add_repo("alice/moved", allowed=["alice"])
        fake.redirect("alice/moved")
        r = _project(client, ALICE, "moved", "https://github.com/alice/moved")
        assert r.status_code == 409 and r.json()["detail"] == ga.ERRORS["repository_unavailable"]
        for bad in ("https://github.com/alice/r?x=1", "git@github.com:alice/r.git", "https://gitlab.com/a/b"):
            r = _project(client, ALICE, "bad", bad)
            assert r.status_code == 400 and r.json()["detail"] == ga.ERRORS["policy_rejected"]
        fake.add_repo("alice/ok", allowed=["alice"])
        assert _project(client, ALICE, "ok", "https://github.com/alice/ok").status_code == 201
        row = m.db.get_member_project(a, "ok")
        managed = Path(row["repo"])
        from harness.storage import repos_dir
        root = repos_dir(m.cfg, a)
        for key, value in (("url.https://evil.example/.insteadOf", "https://github.com/"),
                           ("credential.helper", "!sh -c 'cat > /tmp/stolen'"),
                           ("http.extraHeader", "Authorization: bearer x"), ("http.proxy", "http://evil:1"),
                           ("remote.origin.uploadpack", "touch /tmp/pwned"), ("core.hooksPath", str(tmp_path)),
                           ("filter.x.smudge", "touch /tmp/pwned"), ("core.fsmonitor", "touch /tmp/pwned"),
                           ("remote.origin.url", "https://github.com/evil/repo.git"),
                           ("include.path", str(tmp_path / "x"))):
            _git(managed, "config", key, value)
            calls = len([c for c in fake.git_calls() if "fetch" in c["argv"] or "push" in c["argv"]])
            with pytest.raises(GitHubAuthError) as e:
                m.github_auth.fetch(a, row["source_url"], managed, root)
            assert e.value.code == "policy_rejected", key
            assert len([c for c in fake.git_calls() if "fetch" in c["argv"] or "push" in c["argv"]]) == calls
            if key == "remote.origin.url":
                _git(managed, "config", key, row["source_url"])
            else:
                _git(managed, "config", "--unset-all", key)
        assert m.github_auth.fetch(a, row["source_url"], managed, root) == ""
        # a stored origin that is not canonical, or a repo outside the member root, is refused
        with pytest.raises(GitHubAuthError):
            m.github_auth.fetch(a, "https://github.com/alice/ok", managed, root)
        with pytest.raises(GitHubAuthError):
            m.github_auth.fetch(a, row["source_url"], managed, repos_dir(m.cfg, ids[BOB]))
    finally:
        close(client)


def test_racing_disconnect_stops_inflight_git(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        a = ids[ALICE]
        connect(client, fake, ALICE, a)
        fake.add_repo("alice/slow", allowed=["alice"])
        from harness.storage import repos_dir
        gate = threading.Event()
        started = []
        orig_register = m.github_auth.ops.register

        def register(uid, proc):
            started.append(proc)
            orig_register(uid, proc)
            gate.set()

        m.github_auth.ops.register = register
        # make the clone slow: point the fake at a repo, then disconnect while the process runs
        sleeper = fake.git_dir / "fake_git.py"
        sleeper.write_text("import time\ntime.sleep(30)\n" + sleeper.read_text(encoding="utf-8"), encoding="utf-8")
        result = {}

        def run():
            try:
                m.github_auth.clone(a, "https://github.com/alice/slow", repos_dir(m.cfg, a) / "slow",
                                    repos_dir(m.cfg, a))
            except GitHubAuthError as e:
                result["code"] = e.code

        t = threading.Thread(target=run)
        t.start()
        assert gate.wait(20)
        began = time.monotonic()
        threading.Thread(target=lambda: m.github_auth.disconnect(a)).start()
        t.join(30)
        assert result.get("code") == "cancelled" and time.monotonic() - began < 25
        assert not (repos_dir(m.cfg, a) / "slow").exists()
        assert wait_for(lambda: fake.namespaces() == set())
        assert m.db.get_github_connection(a)["status"] == "disconnected"
    finally:
        close(client)


def test_owner_project_github_flag_refused_and_owner_path_unchanged(tmp_path, fake):
    client, m, ids = setup(tmp_path, fake)
    try:
        r = client.post("/api/v1/projects", json={"name": "own", "repo": "https://github.com/o/r", "github": True},
                        headers=H(OWNER))
        assert r.status_code == 400
        assert fake.git_calls() == [] and not [c for c in fake.gcm_calls() if c["action"] != "store"
                                               and c["env"]["GCM_NAMESPACE"] != ga.PREFLIGHT_NAMESPACE]
    finally:
        close(client)


def test_member_review_push_uses_managed_repo_and_stored_origin(tmp_path, fake):
    """End to end: private clone -> session branch -> reviewed push, never the workspace's own remote."""
    client, m, ids = setup(tmp_path, fake)
    try:
        a = ids[ALICE]
        connect(client, fake, ALICE, a)
        fake.add_repo("alice/app", allowed=["alice"])
        assert _project(client, ALICE, "app", "https://github.com/alice/app").status_code == 201
        row = m.db.get_member_project(a, "app")
        from harness import catalog, clone
        from harness.storage import workspaces_dir
        project = catalog.get_project(m.cfg, m.db, a, "app")
        ws = workspaces_dir(m.cfg, a) / "s-e2e"
        info = clone.isolated_prepare(ws, project.repo, "s-e2e", workspaces_dir(m.cfg, a))
        (ws / "feature.txt").write_text("feature\n", encoding="utf-8")
        # the agent rewrites its workspace remote and adds a hook: neither is used for the push
        _git(ws, "remote", "set-url", "origin", "https://github.com/evil/steal.git")
        hook = ws / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\ntouch pwned\n", encoding="utf-8")
        s = {"id": "s-e2e", "owner_id": a, "branch": info["branch"]}
        from harness import projects
        projects.snapshot(ws, "Work in progress from session s-e2e")
        detail = asyncio.run(m._push_member_github(s, project, ws, row))
        assert detail == f"pushed {info['branch']} to alice/app"
        bare = Path(fake.server["repos"]["alice/app"]["path"])
        assert "feature.txt" in _git(bare, "ls-tree", "--name-only", info["branch"])
        push_call = [c for c in fake.git_calls() if "push" in c["argv"]][-1]
        assert "https://github.com/alice/app.git" in push_call["argv"]
        assert "evil" not in json.dumps(push_call["argv"])
        assert Path(push_call["argv"][push_call["argv"].index("-C") + 1]) == Path(row["repo"])
        assert not (ws / "pwned").exists()
    finally:
        close(client)


def test_helper_relays_only_valid_prompts(tmp_path):
    from harness import gcm_ui_helper
    assert gcm_ui_helper.main(["credentials", "--username", "x"]) == 1
    assert gcm_ui_helper.main(["device", "--code", "WDJB-MJHT", "--url", "https://evil.example/device"]) == 1
    assert gcm_ui_helper.main(["device", "--code", "nope", "--url", "https://github.com/login/device"]) == 1
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    env = {**os.environ, "AGENT_HARNESS_GCM_RELAY": f"127.0.0.1:{port}", "AGENT_HARNESS_GCM_NONCE": "n0nce"}
    proc = subprocess.Popen([sys.executable, "-I", gcm_ui_helper.__file__, "device", "--code", CODE, "--url",
                             "https://github.com/login/device"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    conn, _ = server.accept()
    conn.settimeout(10)
    assert conn.recv(200) == f"n0nce\thttps://github.com/login/device\t{CODE}\n".encode()
    time.sleep(0.5)
    assert proc.poll() is None  # stays alive until GCM kills it or the daemon closes the relay
    conn.close()
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 1 and out == b"" and err == b""
    server.close()


# --- helpers -------------------------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    import shutil
    r = subprocess.run([shutil.which("git"), "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return r.stdout


def _assert_no_secrets(m, fake, tmp_path):
    secrets = list(fake.issued()) + [CODE]
    assert secrets[:-1], "the fake issued no tokens"
    m.db.conn.execute("PRAGMA wal_checkpoint(FULL)")
    scanned = 0
    for path in Path(m.cfg.data_dir).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            scanned += 1
            for secret in secrets:
                assert secret.encode() not in data, f"{secret[:6]}… found in {path}"
    assert scanned
    rows = json.dumps([m.db.list_audit(500), m.db.list_github_connections()])
    for secret in secrets:
        assert secret not in rows
