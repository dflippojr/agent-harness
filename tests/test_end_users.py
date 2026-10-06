"""Per-user subscription logins for App requests (#365): routing, isolation, the popup flows, concurrency, revocation.

The sign-in flows run against stub processes (no provider is contacted); the Docker tests use temporary volumes with
unique names and a network-less container."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import backend_state, cli_backends, cli_domains, credential_sources, end_users
from harness.api import create_app
from harness.cli_backends import ClaudeSession, CliBackendError, CodexSession, ready_domain
from harness.config import BackendConfig, SandboxConfig
from harness.end_users import EndUserLogins
from harness.manager import Manager
from test_daemon import make_cfg, wait_status
from test_phase8 import FAKE_CLAUDE

CANARY = "CANARY-code-7f3a9c1e2b4d#state-5d6e7f"
OWNER_TOKEN = "owner-token-must-never-reach-an-end-user"

# A stub of `claude auth login` (prints the URL and waits for the pasted code on stdin, as the pinned binary does:
# checked against it offline) and of `codex login --device-auth` (prints a URL and a user code, takes nothing back).
STUB_LOGIN = r'''import pathlib, sys, time
mode, marker, expected = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
if mode == "claude":
    print("Opening browser to sign in…", flush=True)
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=abc", flush=True)
    print("Paste code here if prompted > ", end="", flush=True)
    line = sys.stdin.readline()
    marker.with_suffix(".stdin").write_text(line)           # proves the code arrived on stdin
    print("you pasted " + line.strip(), flush=True)          # a CLI that echoes its input must not leak it
    if line.strip() != expected:
        print("Invalid code. Please make sure the full code was copied.", flush=True)
        raise SystemExit(1)
    marker.write_text("linked")
elif mode == "codex":
    print("Open https://auth.openai.com/codex/device and enter the code", flush=True)
    print("\x1b[1mABCD-12345\x1b[0m", flush=True)
    time.sleep(float(expected))
    marker.write_text("linked")
else:  # hang: a sign-in nobody finishes
    print("https://example.invalid/login", flush=True)
    time.sleep(60)
'''


@pytest.fixture
def stub(tmp_path):
    script = tmp_path / "stub_login.py"
    script.write_text(STUB_LOGIN, encoding="utf-8")
    marker = tmp_path / "linked.marker"

    def command_for(mode: str, expected: str = CANARY):
        return lambda backend, app_id, end_user, attempt_id, container: [
            sys.executable, "-u", str(script), mode, str(marker), expected]
    return script, marker, command_for


def _cfg(tmp_path, **claude):
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", max_sessions=3, **claude)
    cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol", max_sessions=3)
    return cfg


def _manager(tmp_path, **claude):
    return Manager(_cfg(tmp_path, **claude))


def _linked_when(monkeypatch, marker: Path):
    monkeypatch.setattr(backend_state, "_probe_subscription",
                        lambda name, cfg, app_id="", end_user="": marker.exists())
    backend_state._eu_cache.clear()


def _app(client, name="planner"):
    app = client.post("/keys", json={"name": name, "kind": "app", "scopes": ["sessions"]}).json()
    return app, {"Authorization": f"Bearer {app['key']}"}


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _durable(manager) -> bytes:
    root = manager.cfg.db_path.parent
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file() and ".sqlite3" in p.name)


# --- routing: the person's own login, or a refusal -----------------------------------------------------------------

def test_a_request_with_an_end_user_is_refused_without_a_login_and_never_falls_back(tmp_path, monkeypatch):
    token = tmp_path / "owner.token"
    token.write_text(OWNER_TOKEN, encoding="utf-8")
    manager = _manager(tmp_path, oauth_token_file=str(token), oauth_token_apps=["*"])
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: False)
    backend_state._eu_cache.clear()
    manager._spawn = lambda *_a, **_k: None
    with TestClient(create_app(manager)) as client:
        app, headers = _app(client)
        manager.set_app_provider_credential(app["id"], "claude", "", "subscription", [])
        refused = client.post("/api/v1/sessions", headers=headers,
                              json={"prompt": "hi", "backend": "claude", "end_user": "dana"})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "end_user_login_required"
        assert manager.db.app_session_ids(app["id"]) == []
        # Without an end user the App's request behaves as before.
        assert client.post("/api/v1/sessions", headers=headers,
                           json={"prompt": "hi", "backend": "claude"}).status_code == 201
        # Only claude and codex, only an App token, only a well-formed id.
        for bad, status in (({"backend": "cursor"}, 400), ({"backend": "local"}, 400), ({"backend": None}, 400),
                            ({"backend": "claude", "end_user": "../x"}, 400), ({"backend": "claude", "end_user": ""}, 201)):
            body = {"prompt": "hi", "end_user": "dana", **bad}
            assert client.post("/api/v1/sessions", headers=headers, json=body).status_code == status, bad


def test_a_linked_end_user_session_records_the_person_and_runs_on_no_other_credential(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: True)
    backend_state._eu_cache.clear()
    manager._spawn = lambda *_a, **_k: None
    secret = tmp_path / "app.key"
    secret.write_text("app-key-plaintext", encoding="utf-8")
    manager.cfg.provider_secret_files = {"billing": str(secret)}
    with TestClient(create_app(manager)) as client:
        app, headers = _app(client)
        manager.set_app_provider_credential(app["id"], "claude", "billing", "subscription_then_api_key", [])
        created = client.post("/api/v1/sessions", headers=headers,
                              json={"prompt": "hi", "backend": "claude", "end_user": "dana"})
        assert created.status_code == 201, created.text
        assert created.json()["end_user"] == "dana"
        s = manager.db.get_session(created.json()["id"])
        assert s["end_user"] == "dana"
        # The App's API key and its fallback never apply to an end user's request.
        credential = manager.runner._backend_credential(s)
        assert (credential["policy"], credential["key"], credential["source"]) == ("subscription", "",
                                                                                    "end_user_login")
        plain = manager.runner._backend_credential({**s, "end_user": ""})
        assert plain["policy"] == "subscription_then_api_key" and plain["key"] == "app-key-plaintext"
        # An App the owner has not granted the backend stays denied.
        manager.set_app_provider_credential(app["id"], "claude", "billing", "api_key", [])
        manager.db.revoke_app_provider_credential(
            manager.db.app_provider_credential(app["id"], "claude")["id"])
        assert manager.runner._backend_credential(s)["policy"] == "denied"


def test_end_user_sessions_get_their_own_volume_and_never_the_owners_login_token_or_a_key(tmp_path):
    token = tmp_path / "owner.token"
    token.write_text(OWNER_TOKEN, encoding="utf-8")
    cfg = BackendConfig(enabled=True, volume="harness-auth-claude", oauth_token_file=str(token),
                        oauth_token_apps=["app-1"], proxy="http://proxy:8888", network="cli-net")
    owner = ClaudeSession(session_id="o", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                          system_prompt="s", app_id="app-1", popen=lambda *a, **k: (_ for _ in ()).throw(SystemExit))
    assert "CLAUDE_CODE_OAUTH_TOKEN" in owner.command()  # the owner's own App uses the token: control
    mine = ClaudeSession(session_id="m", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                         system_prompt="s", app_id="app-1", end_user="dana", api_key="app-key")
    command = mine.command()
    volume = cli_domains.end_user_volume("claude", "app-1", "dana")
    assert ["-v", f"{volume}:/home/agent/.claude"] in [command[i:i + 2] for i in range(len(command) - 1)]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in command and "ANTHROPIC_API_KEY" not in command
    assert not any("harness-login-claude" in a or a.startswith("harness-auth-claude") for a in command)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude/.login" in command
    assert mine.api_key == "" and not mine._uses_token()
    codex = CodexSession(session_id="c", workspace=tmp_path, backend=BackendConfig(volume="harness-auth-codex"),
                         sandbox=SandboxConfig(), system_prompt="s", app_id="app-1", end_user="dana")
    codex_volume = cli_domains.end_user_volume("codex", "app-1", "dana")
    assert f"{codex_volume}:/home/agent/.codex" in codex.command()
    assert codex_volume != volume


def test_the_session_environment_never_carries_the_owner_token_for_an_end_user(tmp_path, monkeypatch):
    token = tmp_path / "owner.token"
    token.write_text(OWNER_TOKEN, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "daemon-env-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "daemon-env-key")
    seen = {}

    def popen(command, **kwargs):
        seen["env"], seen["command"] = kwargs["env"], command
        raise OSError("stop here")

    async def noop(*_a, **_k):
        return 0, "", ""
    monkeypatch.setattr(cli_backends, "run_cmd", noop)
    monkeypatch.setattr(cli_backends, "ready_domain", noop)
    cfg = BackendConfig(enabled=True, oauth_token_file=str(token), oauth_token_apps=["app-1"])
    cli = ClaudeSession(session_id="e", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                        system_prompt="s", app_id="app-1", end_user="dana", popen=popen)
    with pytest.raises(OSError):
        asyncio.run(cli.start())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert OWNER_TOKEN not in str(seen["env"].values())


def test_ready_domain_refuses_before_touching_a_volume_when_the_end_user_has_no_login(monkeypatch):
    calls = []

    async def prepare(*args, **kwargs):
        calls.append(args)
    monkeypatch.setattr(cli_domains, "prepare", prepare)
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: False)
    backend_state._eu_cache.clear()
    with pytest.raises(CliBackendError) as refused:
        asyncio.run(ready_domain("claude", BackendConfig(enabled=True), "app-1", "", "dana"))
    assert refused.value.code == "end_user_login_required" and calls == []
    with pytest.raises(CliBackendError) as unsupported:
        asyncio.run(ready_domain("cursor", BackendConfig(enabled=True), "app-1", "", "dana"))
    assert unsupported.value.code == "end_user_backend_unsupported"
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: True)
    asyncio.run(ready_domain("claude", BackendConfig(enabled=True), "app-1", "", "dana"))
    assert len(calls) == 1 and calls[0][-1] == "dana"


# --- isolation ---------------------------------------------------------------------------------------------------

def test_volumes_are_named_from_a_hash_of_the_app_and_the_end_user_and_never_collide():
    names = {cli_domains.end_user_volume(b, app, user)
             for b in ("claude", "codex") for app in ("a", "a:b", "b") for user in ("x", "b:x", "x:b")}
    assert len(names) == 18
    assert all(n.startswith("harness-eu-") and n.count("/") == 0 for n in names)
    assert cli_domains.end_user_volume("claude", "a", "b:c") != cli_domains.end_user_volume("claude", "a:b", "c")
    assert cli_domains.valid_end_user("dana") and cli_domains.valid_end_user("dana@example.com")
    assert not any(cli_domains.valid_end_user(x) for x in ("", "../x", "a b", "a/b", "-x", "x" * 129, "a\nb"))


def test_the_login_container_mounts_only_the_end_users_volume_and_has_no_secret(tmp_path):
    cfg = _cfg(tmp_path)
    logins = EndUserLogins(cfg)
    command = logins._docker_command("claude", "app-1", "dana", "attempt", "harness-eulogin-x")
    pairs = [command[i:i + 2] for i in range(len(command) - 1)]
    volume = cli_domains.end_user_volume("claude", "app-1", "dana")
    assert [a for a in pairs if a[0] == "-v"] == [["-v", f"{volume}:/home/agent/.claude"]]
    assert command[-3:] == ["claude", "auth", "login"] and "-i" in command and "--rm" in command
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in " ".join(command)
    assert not any(a.endswith("target=/workspace") for a in command)
    # Managed settings and read-only config come along, as in a session.
    assert any("managed-settings.json" in a for a in command)
    codex = logins._docker_command("codex", "app-1", "dana", "attempt", "harness-eulogin-y")
    assert codex[-3:] == ["codex", "login", "--device-auth"]


def test_the_daemon_never_reads_end_user_volumes_host_side():
    source = (Path(end_users.__file__)).read_text(encoding="utf-8")
    import re
    assert not re.search(r"(Path|open|read_text|read_bytes|listdir|glob)\(", source)
    assert "volume_mountpoint" not in source and "Mountpoint" not in source


# --- the Claude popup flow (stub) --------------------------------------------------------------------------------

def test_the_claude_popup_flow_url_out_code_in_once_then_linked(tmp_path, monkeypatch, caplog, stub):
    _, marker, command_for = stub
    manager = _manager(tmp_path)
    manager.end_user_logins = EndUserLogins(manager.cfg, command=command_for("claude"))
    _linked_when(monkeypatch, marker)
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(manager)) as client:
        app, headers = _app(client)
        _, other = _app(client, "other")
        url = "/api/v1/end-users/dana/logins/claude"
        started = client.post(url, headers=headers)
        assert started.status_code == 201, started.text
        first = started.json()
        assert first["verification_url"].startswith("https://claude.com/cai/oauth/authorize")
        assert first["needs_code"] is True and "user_code" not in first and first["attempt_id"]
        status = client.get(url, headers=headers).json()
        assert status["linked"] is False and status["attempt"]["state"] == "waiting"
        assert "verification_url" not in status["attempt"]
        code_url = f"{url}/{first['attempt_id']}/code"
        # Only the App that started the attempt can submit its code; a bad code is refused without echoing it.
        assert client.post(code_url, headers=other, json={"code": CANARY}).status_code == 404
        assert client.post(f"/api/v1/end-users/erin/logins/claude/{first['attempt_id']}/code", headers=headers,
                           json={"code": CANARY}).status_code == 404
        bad = client.post(code_url, headers=headers, json={"code": "a\nb" + CANARY})
        assert bad.status_code == 400 and CANARY not in bad.text
        assert client.post(code_url, headers=headers, json={"code": 5}).status_code == 400
        assert client.post(code_url, headers=headers, content=b"{" + CANARY.encode()).status_code == 400
        sent = client.post(code_url, headers=headers, json={"code": CANARY})
        assert sent.status_code == 200 and sent.json()["state"] == "submitted"
        assert CANARY not in sent.text
        assert _wait(lambda: client.get(url, headers=headers).json()["linked"])
        assert _wait(lambda: client.get(url, headers=headers).json()["attempt"]["state"] == "completed")
        done = client.get(url, headers=headers).json()
        # Single use: a second submission is refused, and the CLI got the code exactly once (on stdin).
        again = client.post(code_url, headers=headers, json={"code": CANARY})
        assert again.status_code == 409 and again.json()["error"]["code"] == "code_already_submitted"
        assert CANARY not in again.text
        assert marker.with_suffix(".stdin").read_text().strip() == CANARY
        everything = [started.text, status, done, sent.text, again.text, bad.text]
        assert all(CANARY not in str(x) for x in everything)
    assert CANARY not in caplog.text
    assert CANARY.encode() not in _durable(manager)
    assert manager.db.for_app(app["id"]).end_users() == ["dana"]
    events = manager.db.conn.execute("SELECT COUNT(*) FROM events WHERE data LIKE ?", (f"%{CANARY}%",)).fetchone()[0]
    assert events == 0


def test_a_wrong_code_fails_the_attempt_and_a_new_attempt_replaces_it(tmp_path, monkeypatch, stub):
    _, marker, command_for = stub
    manager = _manager(tmp_path)
    manager.end_user_logins = EndUserLogins(manager.cfg, command=command_for("claude", expected="the-right-code"))
    _linked_when(monkeypatch, marker)
    with TestClient(create_app(manager)) as client:
        _, headers = _app(client)
        url = "/api/v1/end-users/dana/logins/claude"
        first = client.post(url, headers=headers).json()
        assert client.post(f"{url}/{first['attempt_id']}/code", headers=headers,
                           json={"code": "not-the-right-code"}).status_code == 200
        assert _wait(lambda: client.get(url, headers=headers).json()["attempt"]["state"] == "failed")
        assert client.get(url, headers=headers).json()["linked"] is False
        second = client.post(url, headers=headers).json()
        assert second["attempt_id"] != first["attempt_id"]
        assert client.post(f"{url}/{first['attempt_id']}/code", headers=headers,
                           json={"code": "the-right-code"}).status_code == 409  # a finished attempt takes no code


def test_the_code_expires_with_the_attempt(tmp_path, monkeypatch, stub):
    _, marker, command_for = stub
    monkeypatch.setattr(end_users, "ATTEMPT_SECONDS", 1)
    manager = _manager(tmp_path)
    manager.end_user_logins = EndUserLogins(manager.cfg, command=command_for("hang"))
    _linked_when(monkeypatch, marker)
    with TestClient(create_app(manager)) as client:
        _, headers = _app(client)
        url = "/api/v1/end-users/dana/logins/claude"
        first = client.post(url, headers=headers).json()
        proc = manager.end_user_logins._attempts[first["attempt_id"]].process
        assert _wait(lambda: client.get(url, headers=headers).json()["attempt"]["state"] == "expired", 8)
        assert _wait(lambda: proc.poll() is not None, 5)  # the waiting login was killed
        late = client.post(f"{url}/{first['attempt_id']}/code", headers=headers, json={"code": CANARY})
        assert late.status_code == 409


# --- the Codex device flow (stub) --------------------------------------------------------------------------------

def test_the_codex_device_flow_returns_only_a_url_and_a_user_code_and_completes(tmp_path, monkeypatch, stub):
    _, marker, command_for = stub
    manager = _manager(tmp_path)
    manager.end_user_logins = EndUserLogins(manager.cfg, command=command_for("codex", expected="1"))
    _linked_when(monkeypatch, marker)
    with TestClient(create_app(manager)) as client:
        app, headers = _app(client)
        url = "/api/v1/end-users/dana/logins/codex"
        started = client.post(url, headers=headers)
        assert started.status_code == 201, started.text
        body = started.json()
        assert body["verification_url"] == "https://auth.openai.com/codex/device"
        assert body["user_code"] == "ABCD-12345" and body["needs_code"] is False
        assert client.get(url, headers=headers).json()["linked"] is False
        assert client.post(f"{url}/{body['attempt_id']}/code", headers=headers,
                           json={"code": CANARY}).status_code == 409  # nothing comes back
        assert _wait(lambda: client.get(url, headers=headers).json()["linked"], 10)
        # Cursor is out; an unknown backend and a malformed id are refused.
        assert client.post("/api/v1/end-users/dana/logins/cursor", headers=headers).status_code == 400
        assert client.post("/api/v1/end-users/a%20b/logins/codex", headers=headers).status_code == 400
        # Not for the owner or a device: only an App token manages its end users.
        assert client.post(url).status_code in (401, 403)


def test_a_stuck_login_that_prints_no_url_fails_instead_of_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(end_users, "URL_WAIT_SECONDS", 1)
    script = tmp_path / "silent.py"
    script.write_text("import time; time.sleep(30)", encoding="utf-8")
    logins = EndUserLogins(_cfg(tmp_path), command=lambda *a: [sys.executable, str(script)])
    with pytest.raises(end_users.LoginError) as failed:
        asyncio.run(logins.start("app-1", "dana", "claude"))
    assert failed.value.code == "login_failed"
    logins.close()


# --- concurrency -------------------------------------------------------------------------------------------------

def test_credential_locks_queue_one_credential_and_leave_others_alone():
    async def body():
        locks = credential_sources.KeyedLocks()
        order = []

        async def run(key, tag, hold):
            async with locks.hold(key):
                order.append(f"start-{tag}")
                await asyncio.sleep(hold)
                order.append(f"end-{tag}")
        await asyncio.gather(run("k", 1, 0.1), run("k", 2, 0), run("other", 3, 0.05))
        assert order.index("end-1") < order.index("start-2")        # the same credential queues
        assert order.index("start-3") < order.index("end-1")        # another one doesn't wait
        assert locks._locks == {}                                    # nothing is kept once idle
    asyncio.run(body())


def test_one_end_users_sessions_share_a_credential_so_the_runner_serialises_them(tmp_path):
    runner = _manager(tmp_path).runner
    base = {"backend": "claude", "app_id": "app-1", "end_user": "dana"}
    key = lambda s: credential_sources.select(s["app_id"], s["end_user"]).lock_key(s["backend"], s["app_id"], s["end_user"])  # noqa: E731
    assert key(base) == key(dict(base)) and key(base) != key({**base, "end_user": "erin"})
    assert key(base) != key({**base, "backend": "codex"}) and key(base) != key({**base, "app_id": "app-2"})
    assert runner._credential_lock({"backend": "claude", "app_id": "app-1"}).__class__.__name__ == "nullcontext"


def test_two_concurrent_sessions_of_one_end_user_never_run_at_once(tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    manager = _manager(tmp_path)
    monkeypatch.setattr(backend_state, "_probe_subscription", lambda *_a, **_k: True)

    def factory(**kwargs):
        return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), "cancel", str(tmp_path / "s.jsonl")])
    manager.runner.cli_factory = factory

    with TestClient(create_app(manager)) as client:
        row, headers = _app(client)
    app_row = manager.db.get_api_key(row["id"])

    async def run():
        first = manager.create("one", backend="claude", app=app_row, end_user="dana")["id"]
        await _alive(manager, first)
        second = manager.create("two", backend="claude", app=app_row, end_user="dana")["id"]
        third = manager.create("three", backend="claude", app=app_row, end_user="erin")["id"]
        await _alive(manager, third)                              # another person's session runs beside it
        await asyncio.sleep(0.6)
        assert second not in manager.runner._cli_sessions         # the same person's next one waits its turn
        assert manager.db.get_session(second)["status"] == "queued"
        await manager.cancel(first)
        await _alive(manager, second)                             # and runs once the first has ended
        await manager.cancel(second)
        await manager.cancel(third)
        for sid in (first, second, third):
            await wait_status(manager, sid, "cancelled", "done", "failed")
    asyncio.run(run())


async def _alive(manager, sid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cli = manager.runner._cli_sessions.get(sid)
        if cli is not None and cli.process is not None and cli.process.poll() is None:
            return cli
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {sid} never started")


# --- revocation --------------------------------------------------------------------------------------------------

def test_unlink_runs_the_clis_logout_then_deletes_the_volume(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    calls = []

    async def run_cmd(command, **kwargs):
        calls.append(command)
        return 0, "", ""
    monkeypatch.setattr(end_users, "run_cmd", run_cmd)
    monkeypatch.setattr(cli_domains, "run_cmd", run_cmd)
    monkeypatch.setattr(backend_state, "_volume_exists", lambda volume: True)
    manager._spawn = lambda *_a, **_k: None
    with TestClient(create_app(manager)) as client:
        app, headers = _app(client)
        manager.end_user_logins = EndUserLogins(manager.cfg, command=lambda *a: [sys.executable, "-c", ""])
        assert client.delete("/api/v1/end-users/dana/logins/claude", headers=headers).status_code == 204
    volume = cli_domains.end_user_volume("claude", app["id"], "dana")
    logout, remove = calls
    assert logout[-3:] == ["claude", "auth", "logout"] and f"{volume}:/home/agent/.claude" in logout
    assert remove == ["docker", "volume", "rm", "-f", volume]


def test_app_erase_removes_every_end_users_volumes_and_the_registry(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    removed = []

    async def run_cmd(command, **kwargs):
        removed.append(command)
        return 0, "", ""
    monkeypatch.setattr(cli_domains, "run_cmd", run_cmd)
    with TestClient(create_app(manager)) as client:
        app, _ = _app(client)
        store = manager.db.for_app(app["id"])
        for person in ("dana", "erin"):
            store.register_end_user(person)
        store.register_end_user("dana")  # registering twice is harmless
        assert store.end_users() == ["dana", "erin"]
        asyncio.run(manager.erase_app(app["id"]))
    names = {n for command in removed if command[:4] == ["docker", "volume", "rm", "-f"] for n in command[4:]}
    expected = {v for person in ("dana", "erin") for v in cli_domains.end_user_volumes(app["id"], person)}
    assert expected <= names
    assert not any(cli_domains.end_user_volume("claude", "other-app", "dana") in c for c in removed)


def test_usage_is_tallied_per_end_user(tmp_path):
    manager = _manager(tmp_path)
    manager.db.record_usage("claude", "s1", "app-1", 10, 5, 0.5, "subscription", "end_user_login", "dana")
    manager.db.record_usage("claude", "s2", "app-1", 7, 3, 0.25, "subscription", "end_user_login", "dana")
    manager.db.record_usage("claude", "s3", "app-1", 100, 50, 5.0, "subscription", "end_user_login", "erin")
    tally = manager.db.end_user_usage("app-1", "dana")
    assert (tally["sessions"], tally["prompt_tokens"], tally["completion_tokens"]) == (2, 17, 8)
    assert manager.db.end_user_usage("app-2", "dana")["sessions"] == 0


# --- Docker: temporary volumes, a network-less container ---------------------------------------------------------

IMAGE = "agent-harness-cli:1"
docker_ok = bool(shutil.which("docker")) and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
needs_docker = pytest.mark.skipif(not docker_ok, reason=f"needs Docker and the {IMAGE} image")


def _in_session(backend: str, cfg: BackendConfig, app_id: str, end_user: str, workspace: Path, script: str) -> str:
    kind = ClaudeSession if backend == "claude" else CodexSession
    session = kind(session_id=f"t365-{uuid.uuid4().hex[:8]}", workspace=workspace, backend=cfg,
                   sandbox=SandboxConfig(), system_prompt="s", app_id=app_id, end_user=end_user)
    command = session.command()
    command = command[:command.index(cfg.image) + 1] + ["sh", "-c", script]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


@needs_docker
@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_end_users_cannot_read_each_others_login_or_state_and_the_app_login_is_not_mounted(tmp_path, backend):
    app = f"t365-{uuid.uuid4().hex[:10]}"
    cfg = BackendConfig(enabled=True, image=IMAGE, network="none", volume=f"t365-{uuid.uuid4().hex[:10]}")
    layout = cli_domains.end_user_layout(backend)
    secret_dir = f"{layout.state_dir}/.login" if backend == "claude" else layout.state_dir
    people = ("dana", "erin")
    volumes = {cli_domains.end_user_volume(backend, app, p) for p in people}
    try:
        for person in people:
            subprocess.run(cli_domains.prepare_command(backend, cfg, app, person), check=True, capture_output=True)
            _in_session(backend, cfg, app, person, tmp_path,
                        f"echo CRED-{person} > {secret_dir}/credential && echo HIST-{person} > {layout.state_dir}/h")
        grep = "grep -rsh --exclude-dir=.cache -e CRED- -e HIST- /home/agent"
        for person in people:
            seen = _in_session(backend, cfg, app, person, tmp_path, grep).split()
            assert sorted(seen) == sorted([f"CRED-{person}", f"HIST-{person}"]), (person, seen)
        # Config stays read-only over the end user's state, and no shared login directory exists.
        out = _in_session(backend, cfg, app, "dana", tmp_path,
                          f"(echo X >> {layout.state_dir}/{layout.ro_files[0][0]}) 2>/dev/null && echo WROTE; "
                          "ls -d /home/agent/.claude-login 2>/dev/null; true")
        assert "WROTE" not in out and ".claude-login" not in out
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", *volumes], capture_output=True)


@needs_docker
def test_unlink_deletes_the_volume_with_the_real_cli_logout(tmp_path):
    app = f"t365-{uuid.uuid4().hex[:10]}"
    cfg = _cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, image=IMAGE, network="none", proxy="http://127.0.0.1:9")
    volume = cli_domains.end_user_volume("claude", app, "dana")
    try:
        subprocess.run(cli_domains.prepare_command("claude", cfg.backends["claude"], app, "dana"), check=True,
                       capture_output=True)
        _in_session("claude", cfg.backends["claude"], app, "dana", tmp_path, "echo secret > /home/agent/.claude/.login/c")
        assert subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode == 0
        asyncio.run(EndUserLogins(cfg).unlink(app, "dana", "claude"))
        assert subprocess.run(["docker", "volume", "inspect", volume], capture_output=True).returncode != 0
        asyncio.run(EndUserLogins(cfg).unlink(app, "dana", "claude"))   # unlinking twice is harmless
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


@needs_docker
def test_the_pinned_claude_binary_takes_the_pasted_code_on_stdin(tmp_path):
    """The real `claude auth login` in a network-less throwaway container: URL out, a canary code in once. No
    provider is reached and no account is touched; the code is refused, which is all an offline login can do."""
    app = f"t365-{uuid.uuid4().hex[:10]}"
    cfg = _cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, image=IMAGE, network="none", proxy="http://127.0.0.1:9")
    volume = cli_domains.end_user_volume("claude", app, "dana")
    logins = EndUserLogins(cfg)
    try:
        async def flow():
            started = await logins.start(app, "dana", "claude")
            assert started["verification_url"].startswith("https://") and started["needs_code"] is True
            sent = await logins.submit_code(app, "dana", "claude", started["attempt_id"], CANARY)
            assert sent["state"] == "submitted"
            proc = logins._attempts[started["attempt_id"]].process
            await asyncio.to_thread(proc.wait, 60)
            assert _wait(lambda: logins.attempt_state(app, "dana", "claude")["state"] in ("failed", "completed"), 10)
            assert CANARY not in str(logins.attempt_state(app, "dana", "claude"))
        asyncio.run(flow())
    finally:
        logins.close()
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
