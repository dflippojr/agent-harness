"""Per-member API keys for hosted Claude Code and Codex (#393): storage, routing, isolation and the canary.

No provider is contacted (the key check is a stub) and no container starts (sessions are built, not run)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import sqlite3

import pytest

from harness import cli_backends, cli_domains, credential_sources, member_keys
from harness.cli_backends import ClaudeSession, CliBackendError, CodexSession
from harness.config import BackendConfig, SandboxConfig
from harness.admin import PREFIX
from harness.manager import HarnessError
from harness.member_keys import MemberKeys
from test_household import ALICE, BOB, OWNER, H, create_member, household

CANARY = "sk-ant-api03-CANARYcanary7f3a9c1e2b4d5d6e7f"
OTHER = "sk-ant-api03-OTHERother0a1b2c3d4e5f6a7b8c"
OAI = "sk-proj-CANARYopenai0a1b2c3d4e5f6a7b8c9d"
OWNER_TOKEN = "owner-token-must-never-reach-a-member"
OWNER_KEY = "owner-key-must-never-reach-a-member"
KEYS = "/api/v1/me/api-keys"


@contextlib.contextmanager
def setup(tmp_path, monkeypatch):
    client, m = household(tmp_path)
    token, key = tmp_path / "owner.token", tmp_path / "owner.key"
    token.write_text(OWNER_TOKEN, encoding="utf-8")
    key.write_text(OWNER_KEY, encoding="utf-8")
    m.cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", max_sessions=3, auth="api_key",
                                              api_key_file=str(key), oauth_token_file=str(token))
    m.cfg.backends["codex"] = BackendConfig(enabled=True, model="gpt-5.6-sol", max_sessions=3)
    m.member_keys._probe = lambda _backend, k: (k == CANARY, "the provider rejected this key")
    monkeypatch.setattr(m, "_spawn", lambda sid: None)   # sessions are created, not run
    client.__enter__()
    try:
        ids = {login: create_member(client, login, login.split("@")[0].title())["user_id"] for login in (ALICE, BOB)}
        yield client, m, ids
    finally:
        client.__exit__(None, None, None)


def put(client, login, backend, key):
    return client.put(f"{KEYS}/{backend}", json={"key": key}, headers=H(login))


# --- storage -----------------------------------------------------------------------------------------------------

def test_the_key_is_sealed_in_the_database_and_bound_to_its_member(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        a, b = ids[ALICE], ids[BOB]
        assert put(client, ALICE, "claude", CANARY).status_code == 200
        raw = sqlite3.connect(m.cfg.db_path).execute("SELECT ciphertext, last4 FROM member_api_keys").fetchall()
        assert len(raw) == 1 and CANARY.encode() not in bytes(raw[0][0]) and raw[0][1] == CANARY[-4:]
        assert m.member_keys.get(a, "claude") == CANARY
        assert m.member_keys.get(b, "claude") == ""
        # The row moved to another member does not open: the ciphertext is bound to (member, backend).
        row = m.db.member_api_key(a, "claude")
        m.db.set_member_api_key(b, "claude", bytes(row["ciphertext"]), "xxxx")
        assert m.member_keys.get(b, "claude") == ""
        # A lost or replaced master key leaves the member without a key rather than with a wrong one.
        (m.cfg.data_dir / member_keys.KEY_FILE).unlink()
        fresh = MemberKeys(m.cfg, m.db)
        assert fresh.get(a, "claude") == ""


def test_a_canary_key_never_reaches_responses_logs_events_or_the_database_in_clear(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        seen = [put(client, ALICE, "claude", CANARY), client.get(KEYS, headers=H(ALICE)),
                client.post(f"{KEYS}/claude/test", headers=H(ALICE)),
                put(client, ALICE, "claude", "x"), client.put(f"{KEYS}/claude", content=CANARY, headers=H(ALICE))]
        s = client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "claude"}, headers=H(ALICE)).json()
        seen += [client.get(f"/api/v1/sessions/{s['id']}", headers=H(ALICE))]
        for r in seen:
            assert CANARY not in r.text and CANARY not in json.dumps(dict(r.headers))
        body = client.get(KEYS, headers=H(ALICE)).json()
        claude = next(k for k in body["keys"] if k["backend"] == "claude")
        assert claude["configured"] and claude["last4"] == CANARY[-4:]
        assert "may incur provider API charges" in body["billing_warning"]
        assert CANARY not in caplog.text
        for table in ("sessions", "events", "audit", "usage"):
            try:
                rows = sqlite3.connect(m.cfg.db_path).execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            assert CANARY not in repr(rows), table
        assert CANARY.encode() not in m.cfg.db_path.read_bytes()


def test_the_key_endpoints_are_for_the_signed_in_member_alone(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert client.get(KEYS, headers=H(OWNER)).status_code == 403
        assert client.put(f"{KEYS}/claude", json={"key": CANARY}, headers=H(OWNER)).status_code == 403
        assert client.put(f"{KEYS}/cursor", json={"key": CANARY}, headers=H(ALICE)).status_code == 400
        assert put(client, ALICE, "claude", "short").status_code == 400
        assert put(client, ALICE, "claude", CANARY).status_code == 200
        # Bob sees nothing of Alice's key, and cannot test or delete it.
        bob = client.get(KEYS, headers=H(BOB)).json()
        assert not any(k["configured"] for k in bob["keys"])
        assert client.post(f"{KEYS}/claude/test", headers=H(BOB)).status_code == 409
        assert client.delete(f"{KEYS}/claude", headers=H(BOB)).status_code == 200
        assert m.member_keys.has(ids[ALICE], "claude")
        ok = client.post(f"{KEYS}/claude/test", headers=H(ALICE)).json()
        assert ok["ok"] is True
        assert put(client, ALICE, "claude", OTHER).status_code == 200
        bad = client.post(f"{KEYS}/claude/test", headers=H(ALICE)).json()
        assert bad["ok"] is False and OTHER not in json.dumps(bad)


# --- sessions ----------------------------------------------------------------------------------------------------

def test_a_member_without_a_key_has_hosted_backends_unavailable_and_never_the_owners_credentials(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        r = client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "claude"}, headers=H(ALICE))
        assert r.status_code == 403 and r.json()["error"]["code"] == "member_api_key_required"
        assert "API key" in r.json()["error"]["message"]
        assert client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "cursor"}, headers=H(ALICE)).status_code == 403
        # A session that somehow reaches the runner has a denied credential, whatever the owner configured.
        s = {"backend": "claude", "app_id": "", "end_user": member_keys.end_user_id(ids[ALICE]), "run": {}}
        credential = m.runner._backend_credential(s)
        assert credential["policy"] == "denied" and credential["key"] == "" and credential["source"] == "member_api_key"
        with pytest.raises(CliBackendError) as e:
            m.runner._cli_credentials("sid", s, "claude")
        assert e.value.code == member_keys.REQUIRED
        # The source refuses before a container exists.
        source = credential_sources.select("", s["end_user"])
        assert source is member_keys.SOURCE
        with pytest.raises(credential_sources.CredentialRefused) as refused:
            asyncio.run(source.ready("claude", m.cfg.backends["claude"], "", s["end_user"]))
        assert refused.value.code == member_keys.REQUIRED
        # The local model still works.
        assert client.post("/api/v1/sessions", json={"prompt": "hi"}, headers=H(ALICE)).status_code == 201


def test_a_member_with_a_key_runs_on_that_key_alone(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert put(client, ALICE, "claude", CANARY).status_code == 200
        assert put(client, BOB, "claude", OTHER).status_code == 200
        r = client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "claude"}, headers=H(ALICE))
        assert r.status_code == 201, r.text
        row = m.db.get_session(r.json()["id"])
        assert row["end_user"] == member_keys.end_user_id(ids[ALICE]) and row["app_id"] == ""
        credential = m.runner._backend_credential(row)
        assert (credential["policy"], credential["key"], credential["source"]) == ("api_key", CANARY, "member_api_key")
        # The owner's key and Bob's are not in play.
        assert OWNER_KEY not in json.dumps(credential) and OTHER not in json.dumps(credential)
        # Codex needs its own key.
        assert client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "codex"},
                           headers=H(ALICE)).status_code == 403
        assert put(client, ALICE, "codex", OAI).status_code == 200
        assert client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "codex"},
                           headers=H(ALICE)).status_code == 201


def test_the_docker_args_carry_only_the_members_key_and_their_own_volume(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        cfg = m.cfg.backends["claude"]
        cfg.oauth_token_apps = [""]   # even a configuration that hands the owner's token to Web
        mine = member_keys.end_user_id(ids[ALICE])
        theirs = member_keys.end_user_id(ids[BOB])

        def session(end_user, backend="claude"):
            cls = ClaudeSession if backend == "claude" else CodexSession
            return cls(session_id="s", workspace=tmp_path, backend=m.cfg.backends[backend], sandbox=SandboxConfig(),
                       system_prompt="p", end_user=end_user, api_key=CANARY)

        a, b = session(mine), session(theirs)
        command = a.command()
        assert a.api_key == CANARY and not a._uses_token()  # type: ignore[attr-defined]
        assert "ANTHROPIC_API_KEY" in command and CANARY not in command and "CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert not any("harness-login-claude" in x or x.startswith("harness-auth-claude") for x in command)
        assert f"{cli_domains.end_user_volume('claude', '', mine)}:/home/agent/.claude" in command
        assert cli_domains.end_user_volume("claude", "", mine) != cli_domains.end_user_volume("claude", "", theirs)
        assert cli_domains.end_user_volume("claude", "", mine) not in b.command()
        codex = session(mine, "codex").command()
        assert "OPENAI_API_KEY" in codex and CANARY not in codex
        assert f"{cli_domains.end_user_volume('codex', '', mine)}:/home/agent/.codex" in codex
        # An App's end user still never gets a key.
        assert ClaudeSession(session_id="s", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                             system_prompt="p", app_id="app-1", end_user="dana", api_key=CANARY).api_key == ""


def test_the_environment_of_a_member_session_holds_their_key_and_no_owner_credential(tmp_path, monkeypatch):
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
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        session = ClaudeSession(session_id="s", workspace=tmp_path, backend=m.cfg.backends["claude"],
                                sandbox=SandboxConfig(), system_prompt="p", popen=popen, api_key=CANARY,
                                end_user=member_keys.end_user_id(ids[ALICE]))
        with pytest.raises(OSError):
            asyncio.run(session.start())
    assert seen["env"]["ANTHROPIC_API_KEY"] == CANARY and "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert CANARY not in seen["command"]


def test_an_app_cannot_name_a_member_as_its_end_user(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert not cli_domains.valid_end_user(member_keys.end_user_id(ids[ALICE]))
        assert cli_domains.valid_end_user("dana")
        app = {"id": "app-1", "name": "x"}
        with pytest.raises(HarnessError) as e:
            m.check_end_user(member_keys.end_user_id(ids[ALICE]), app, "claude")
        assert e.value.code == "invalid_end_user"
        # And the member source never selects an App's request, nor does the end-user source select a member.
        assert credential_sources.select("", member_keys.end_user_id(ids[ALICE])) is member_keys.SOURCE
        assert credential_sources.select("app-1", "dana") is not member_keys.SOURCE


# --- removal -----------------------------------------------------------------------------------------------------

def test_deleting_the_key_stops_new_sessions_and_removes_it(tmp_path, monkeypatch):
    removed = []

    async def fake_drop(end_users_app, users, backends=()):
        removed.append((end_users_app, users, tuple(backends)))
    monkeypatch.setattr(cli_domains, "drop_end_user_volumes", fake_drop)
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert put(client, ALICE, "claude", CANARY).status_code == 200
        assert client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "claude"},
                           headers=H(ALICE)).status_code == 201
        assert client.delete(f"{KEYS}/claude", headers=H(ALICE)).status_code == 200
        assert m.db.member_api_keys(ids[ALICE]) == []
        assert removed == [("", [member_keys.end_user_id(ids[ALICE])], ("claude",))]
        r = client.post("/api/v1/sessions", json={"prompt": "hi", "backend": "claude"}, headers=H(ALICE))
        assert r.status_code == 403 and r.json()["error"]["code"] == "member_api_key_required"


def test_disabling_a_member_deletes_their_keys(tmp_path, monkeypatch):
    async def fake_drop(*_a, **_k):
        return None
    monkeypatch.setattr(cli_domains, "drop_end_user_volumes", fake_drop)
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        assert put(client, ALICE, "claude", CANARY).status_code == 200
        assert put(client, BOB, "claude", OTHER).status_code == 200
        r = client.patch(f"{PREFIX}/accounts/{ids[ALICE]}", json={"enabled": False}, headers=H(OWNER))
        assert r.status_code == 200, r.text
        assert m.db.member_api_keys(ids[ALICE]) == []
        assert m.member_keys.get(ids[BOB], "claude") == OTHER


# --- accounting --------------------------------------------------------------------------------------------------

def test_a_members_usage_is_tallied_on_their_own_id(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        mine = member_keys.end_user_id(ids[ALICE])
        m.db.record_usage("claude", "s1", "", 100, 20, 0.5, "api_key", "member_api_key", mine)
        m.db.record_usage("claude", "s2", "", 7, 3, 0.1, "api_key", "member_api_key", member_keys.end_user_id(ids[BOB]))
        body = client.get(KEYS, headers=H(ALICE)).json()
        assert body["usage"]["claude"] == {"sessions": 1, "prompt_tokens": 100, "completion_tokens": 20}
        assert body["usage"]["codex"]["sessions"] == 0


def test_the_members_card_is_in_the_web_ui():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "harness" / "web" / "pages"
    profile = (root / "profile.mjs").read_text(encoding="utf-8")
    card = profile[profile.index("function apiKeysCard("):profile.index("const GITHUB_DEVICE_URL")]
    for needed in ('"/me/api-keys"', "`/me/api-keys/${k.backend}`", '"PUT"', '"DELETE"', "/test", 'type: "password"', "k.last4"):
        assert needed in card, needed
    for forbidden in ("localStorage", "sessionStorage", "storeSet"):
        assert forbidden not in card
    assert "isMember() ? apiKeysCard() : null" in profile
    assert '"/me/api-keys"' in (root / "new-task.mjs").read_text(encoding="utf-8")


def test_a_members_hosted_session_runs_through_the_runner_on_their_key(tmp_path, monkeypatch):
    import sys
    from test_daemon import wait_status
    from test_phase8 import FAKE_CLAUDE
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    started = {}

    with setup(tmp_path, monkeypatch) as (client, m, ids):
        monkeypatch.undo()                      # sessions run for real this time (the stubs above are not needed)
        m.member_keys._probe = lambda *_a: (True, "")

        def factory(**kwargs):
            session = ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), "echo", str(tmp_path / "s.jsonl")])
            started.update(api_key=session.api_key, end_user=kwargs.get("end_user"), app_id=kwargs.get("app_id"))
            return session
        m.runner.cli_factory = factory
        m.runner._backend_slots = {"claude": asyncio.Semaphore(3)}
        assert put(client, ALICE, "claude", CANARY).status_code == 200

        async def run():
            sid = m.create("hi", backend="claude", owner_id=ids[ALICE])["id"]
            await wait_status(m, sid, "done", "failed", "cancelled")
            return sid
        sid = asyncio.run(run())
        assert started == {"api_key": CANARY, "end_user": member_keys.end_user_id(ids[ALICE]), "app_id": ""}
        run_row = m.db.get_session(sid)["run"]
        assert run_row["credential_source"] == "member_api_key" and run_row["billing_mode"] == "api_key"


def test_a_partial_master_key_file_is_replaced_not_trusted(tmp_path, monkeypatch):
    with setup(tmp_path, monkeypatch) as (client, m, ids):
        path = m.cfg.data_dir / member_keys.KEY_FILE
        path.write_bytes(b"")                       # a crash between create and write
        fresh = MemberKeys(m.cfg, m.db)
        fresh.set(ids[ALICE], "claude", CANARY)
        assert fresh.get(ids[ALICE], "claude") == CANARY
        assert len(base64.b64decode(path.read_bytes())) == 32
        assert not list(path.parent.glob(f"{member_keys.KEY_FILE}.*.tmp"))
        assert MemberKeys(m.cfg, m.db).get(ids[ALICE], "claude") == CANARY
