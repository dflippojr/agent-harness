"""The owner's long-lived Claude token (#390): sessions get CLAUDE_CODE_OAUTH_TOKEN and no shared login to race."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from harness import claude_token, cli_domains
from harness.cli_backends import ClaudeSession, CliBackendError, ready_domain
from harness.config import BackendConfig, SandboxConfig

SECRET = "sk-ant-oat01-THE-SECRET-VALUE"


def _cfg(tmp_path, apps=(), expires="2027-10-05") -> BackendConfig:
    token = tmp_path / "claude-oauth-token"
    token.write_text(SECRET + "\n", encoding="utf-8")
    if expires:
        (tmp_path / "claude-oauth-token.expires").write_text(expires, encoding="utf-8")
    return BackendConfig(enabled=True, volume="harness-auth-claude", network="harness-cli-claude",
                         oauth_token_file=str(token), oauth_token_apps=list(apps))


class _Proc:
    stdin = stdout = stderr = None

    def __init__(self):
        self.stdout = iter(())
        self.stderr = iter(())

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _session(cfg, tmp_path, app_id="", api_key="", popen=None):
    return ClaudeSession(session_id="s1", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                         system_prompt="system", app_id=app_id, api_key=api_key,
                         **({"popen": popen} if popen else {}))


def test_token_sessions_mount_no_login_volume_and_pass_the_token_by_name(tmp_path):
    cfg = _cfg(tmp_path)
    for app in ("", "k-owner"):
        command = _session(_cfg(tmp_path, apps=["k-owner"]), tmp_path, app_id=app).command()
        assert not any("harness-login-claude" in a for a in command)
        assert not any(a.startswith("CLAUDE_SECURESTORAGE_CONFIG_DIR=") for a in command)
        assert ["-e", claude_token.ENV] in [command[i:i + 2] for i in range(len(command) - 1)]
        assert not any(SECRET in a for a in command)
    assert cfg.oauth_token_file


def test_two_concurrent_sessions_share_nothing_writable_that_holds_a_credential(tmp_path):
    cfg = _cfg(tmp_path)
    a = _session(cfg, tmp_path).command()
    b = _session(cfg, tmp_path).command()
    for command in (a, b):
        volumes = [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]
        assert volumes == ["harness-auth-claude:/home/agent/.claude"]  # state only; the login volume is gone


def test_without_a_token_file_the_login_volume_is_still_used(tmp_path):
    cfg = BackendConfig(enabled=True, volume="harness-auth-claude", network="harness-cli-claude")
    command = _session(cfg, tmp_path).command()
    assert "harness-login-claude:/home/agent/.claude-login" in command
    assert claude_token.ENV not in command


def test_the_token_reaches_the_docker_client_environment_only(tmp_path, monkeypatch):
    monkeypatch.setenv(claude_token.ENV, "stale-from-the-daemon-environment")
    seen = {}

    def popen(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return _Proc()

    cfg = _cfg(tmp_path)
    session = _session(cfg, tmp_path, popen=popen)
    session._command_override = None

    async def go():
        # skip docker: only the env handling of start() is under test
        monkeypatch.setattr("harness.cli_backends.run_cmd", lambda *a, **k: _done((0, "", "")))
        monkeypatch.setattr("harness.cli_backends.ready_domain", lambda *a, **k: _done(None))
        try:
            await session.start()
        except Exception:
            pass
    asyncio.run(go())
    assert seen["env"][claude_token.ENV] == SECRET
    assert SECRET not in " ".join(seen["command"])


async def _done(value):
    return value


def test_an_api_key_session_does_not_get_the_token(tmp_path):
    command = _session(_cfg(tmp_path), tmp_path, api_key="k").command()
    assert claude_token.ENV not in command
    assert "ANTHROPIC_API_KEY" in command


def test_other_apps_are_refused_with_a_pointer_to_api_key_billing(tmp_path):
    cfg = _cfg(tmp_path, apps=["k-owner"])
    asyncio.run(_ready(cfg, "k-owner"))
    with pytest.raises(CliBackendError, match="API key"):
        asyncio.run(_ready(cfg, "k-member-app"))
    asyncio.run(_ready(cfg, "k-member-app", api_key="key"))  # API-key billing is the way to serve other people


async def _ready(cfg, app_id, api_key=""):
    import harness.cli_domains as domains

    async def prepared(*a, **k):
        return None
    original = domains.prepare
    domains.prepare = prepared
    try:
        await ready_domain("claude", cfg, app_id, api_key)
    finally:
        domains.prepare = original


def test_missing_token_file_stops_the_session_without_naming_the_path(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "claude-oauth-token").write_text("", encoding="utf-8")
    session = _session(cfg, tmp_path)

    async def go():
        import harness.cli_backends as b
        orig = (b.run_cmd, b.ready_domain)
        b.run_cmd = lambda *a, **k: _done((0, "", ""))
        b.ready_domain = lambda *a, **k: _done(None)
        try:
            await session.start()
        finally:
            b.run_cmd, b.ready_domain = orig
    with pytest.raises(CliBackendError) as e:
        asyncio.run(go())
    assert str(tmp_path) not in str(e.value)


def test_expiry_and_the_30_day_reminder(tmp_path):
    cfg = _cfg(tmp_path, expires="2026-11-01")
    path = cfg.oauth_token_file
    assert claude_token.expiry(path) == date(2026, 11, 1)
    assert claude_token.reminder(path, today=date(2026, 10, 1)) == ""  # 31 days left
    note = claude_token.reminder(path, today=date(2026, 10, 2))  # 30 days left
    assert "2026-11-01" in note and SECRET not in note
    assert "expired" in claude_token.reminder(path, today=date(2026, 11, 2))
    assert claude_token.reminder(str(tmp_path / "none"), today=date(2026, 10, 1)) == ""


def test_doctor_reports_presence_and_expiry_but_never_the_value(tmp_path):
    from harness import doctor
    from types import SimpleNamespace

    class Report:
        def __init__(self):
            self.lines = []

        def ok(self, name, msg):
            self.lines.append(("ok", name, msg))

        def warn(self, name, msg):
            self.lines.append(("warn", name, msg))

    r = Report()
    doctor.check_claude_token(r, SimpleNamespace(backends={"claude": _cfg(tmp_path, expires="2099-01-01")}))
    assert r.lines == [("ok", "Claude token", "present, expires 2099-01-01")]
    assert SECRET not in repr(r.lines) and str(tmp_path) not in repr(r.lines)
