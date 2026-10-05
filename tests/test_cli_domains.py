"""Per-domain hosted-CLI state (#371): volume names, mounts, read-only config, history erase and per-App logins."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from harness import backend_state, cli_domains
from harness.cli_backends import ClaudeSession, CliBackendError, CodexSession, CursorSession, ready_domain
from harness.cli_erase import erase
from harness.config import BackendConfig, SandboxConfig

SESSIONS = {"claude": ClaudeSession, "codex": CodexSession, "cursor": CursorSession}


def _standard(backend: str) -> BackendConfig:
    return BackendConfig(enabled=True, volume=f"harness-auth-{backend}", network=f"harness-cli-{backend}")


def _pairs(command: list[str]) -> list[list[str]]:
    return [command[at:at + 2] for at in range(len(command) - 1)]


def test_volume_names_give_each_app_its_own_state_and_keep_web_on_the_existing_volume():
    for backend in ("claude", "codex", "cursor"):
        cfg = _standard(backend)
        assert cli_domains.state_volume(backend, cfg) == f"harness-auth-{backend}"
        assert cli_domains.state_volume(backend, cfg, "k-1") == f"harness-cli-{backend}-app-k-1"
    # Claude and Cursor share one login; Codex's login lives in each domain's own volume.
    assert cli_domains.login_volume("claude", _standard("claude"), "k-1") == "harness-login-claude"
    assert cli_domains.login_volume("cursor", _standard("cursor")) == "harness-login-cursor"
    assert cli_domains.login_volume("codex", _standard("codex")) == "harness-auth-codex"
    assert cli_domains.login_volume("codex", _standard("codex"), "k-1") == "harness-cli-codex-app-k-1"
    assert cli_domains.needs_app_login("codex") and not cli_domains.needs_app_login("claude")
    # A configured non-standard name derives the rest from it.
    custom = BackendConfig(volume="t-vol")
    assert cli_domains.state_volume("claude", custom, "k-1") == "t-vol-app-k-1"
    assert cli_domains.login_volume("claude", custom) == "t-vol-login"


def test_unsafe_app_ids_are_hashed_into_distinct_volume_names():
    assert cli_domains.domain_slug("k-1") == "k-1"
    hashed = {cli_domains.domain_slug(app) for app in ("Upper", "a/b", "x" * 60, "../web", "h-" + "0" * 24)}
    assert len(hashed) == 5
    assert all(slug.startswith("h-") and len(slug) == 26 for slug in hashed)
    assert cli_domains.domain_slug("Upper") == cli_domains.domain_slug("Upper")


def test_mounts_put_config_read_only_over_the_domain_state():
    claude = cli_domains.docker_args("claude", _standard("claude"), "k-1")
    assert ["-v", "harness-cli-claude-app-k-1:/home/agent/.claude"] in _pairs(claude)
    assert ["-v", "harness-login-claude:/home/agent/.claude-login"] in _pairs(claude)
    assert ["-e", "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude-login"] in _pairs(claude)
    mounts = [claude[at + 1] for at, arg in enumerate(claude) if arg == "--mount"]
    assert any(m.endswith("target=/home/agent/.claude/settings.json,readonly") for m in mounts)
    assert any(m.endswith("target=/home/agent/.claude/CLAUDE.md,readonly") for m in mounts)
    for d in ("agents", "commands", "skills", "plugins", "hooks", "rules"):
        assert f"type=tmpfs,target=/home/agent/.claude/{d},tmpfs-mode=0555" in mounts
    for m in mounts:
        if m.startswith("type=bind"):
            assert Path(m.split("source=")[1].split(",target=")[0]).is_file()
    codex = cli_domains.docker_args("codex", _standard("codex"), "k-1")
    assert [a for a in codex if a.startswith("harness-") and ":" in a] == [
        "harness-cli-codex-app-k-1:/home/agent/.codex"]
    codex_mounts = " ".join(codex)
    for f in ("config.toml", "AGENTS.md", "AGENTS.override.md", "hooks.json"):
        assert f"target=/home/agent/.codex/{f},readonly" in codex_mounts
    for d in ("rules", "skills", "prompts"):
        assert f"target=/home/agent/.codex/{d},tmpfs-mode=0555" in codex_mounts
    cursor = cli_domains.docker_args("cursor", _standard("cursor"))
    assert ["-v", "harness-auth-cursor:/home/agent/.cursor-state"] in _pairs(cursor)
    assert ["-v", "harness-login-cursor:/home/agent/.config/cursor"] in _pairs(cursor)
    assert not any(a.startswith("HOME=") for a in cursor)
    assert "target=/home/agent/.cursor-state/config/permissions.json,readonly" in " ".join(cursor)


def test_sessions_mount_their_own_domain_and_web_keeps_resuming(tmp_path):
    for backend, cls in SESSIONS.items():
        cfg = _standard(backend)
        state = cli_domains.LAYOUTS[backend].state_dir
        web = cls(session_id="s1", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                  system_prompt="system", backend_session_id="resume-me")
        command = web.command("go") if backend == "cursor" else web.command()
        assert ["-v", f"harness-auth-{backend}:{state}"] in _pairs(command)
        if backend != "codex":  # Codex resumes over its JSON-RPC protocol
            assert ["--resume", "resume-me"] in _pairs(command)
        app = cls(session_id="s2", workspace=tmp_path, backend=cfg, sandbox=SandboxConfig(),
                  system_prompt="system", app_id="k-a")
        command = app.command("go") if backend == "cursor" else app.command()
        assert ["-v", f"harness-cli-{backend}-app-k-a:{state}"] in _pairs(command)
        assert not any(arg.startswith(f"harness-auth-{backend}:") for arg in command)


def test_prepare_hands_new_volumes_to_the_agent_user():
    command = cli_domains.prepare_command("cursor", _standard("cursor"), "k-1")
    assert command[:8] == ["docker", "run", "--rm", "--network", "none", "--user", "0:0", "-v"]
    script = command[-1]
    assert "mkdir -p /state /state/config /state/data /login" in script
    assert "chown 1000:1000 /state /state/config /state/data /login" in script
    assert "harness-login-cursor:/login" in command
    codex = cli_domains.prepare_command("codex", _standard("codex"), "k-1")
    assert "/login" not in codex[-1]


def _conversation_tree(root: Path, keep: str, gone: str) -> None:
    for conv in (keep, gone):
        (root / "projects" / "-workspace").mkdir(parents=True, exist_ok=True)
        (root / "projects" / "-workspace" / f"{conv}.jsonl").write_text("{}\n")
        (root / "projects" / "-workspace" / conv / "subagents").mkdir(parents=True)
        (root / "file-history" / conv).mkdir(parents=True)
        (root / "sessions" / "2026" / "10" / "04").mkdir(parents=True, exist_ok=True)
        (root / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T00-00-00-{conv}.jsonl").write_text("{}\n")
        (root / "config" / "chats" / "abc" / conv).mkdir(parents=True)
        (root / "config" / "chats" / "abc" / conv / "store.db").write_bytes(b"x")
    (root / "history.jsonl").write_text(
        json.dumps({"sessionId": keep, "display": "keep"}) + "\n" + json.dumps({"session_id": gone}) + "\n")
    (root / "session_index.jsonl").write_text(json.dumps({"id": gone}) + "\n")
    (root / ".credentials.json").write_text("secret")
    db = sqlite3.connect(root / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT, title TEXT)")
    db.execute("CREATE TABLE thread_goals (thread_id TEXT, goal TEXT)")
    db.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
    db.execute("CREATE TABLE other (id TEXT)")
    db.executemany("INSERT INTO threads VALUES (?, 't')", [(keep,), (gone,)])
    db.executemany("INSERT INTO thread_goals VALUES (?, 'g')", [(keep,), (gone,)])
    db.executemany("INSERT INTO thread_spawn_edges VALUES (?, ?)", [(keep, gone), (gone, keep), (keep, keep)])
    db.execute("INSERT INTO other VALUES (?)", (gone,))
    db.commit()
    db.close()


def test_erase_removes_one_conversation_and_nothing_else(tmp_path):
    keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
    _conversation_tree(tmp_path, keep, gone)
    assert erase(str(tmp_path), gone) > 0
    remaining = "\n".join(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert gone not in remaining
    for kept in (f"projects/-workspace/{keep}.jsonl", f"projects/-workspace/{keep}/subagents", f"file-history/{keep}",
                 f"sessions/2026/10/04/rollout-2026-10-04T00-00-00-{keep}.jsonl", f"config/chats/abc/{keep}/store.db"):
        assert (tmp_path / kept).exists(), kept
    assert gone not in (tmp_path / "history.jsonl").read_text() and keep in (tmp_path / "history.jsonl").read_text()
    assert (tmp_path / "session_index.jsonl").read_text() == ""
    assert (tmp_path / ".credentials.json").read_text() == "secret"
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    assert db.execute("SELECT id FROM threads").fetchall() == [(keep,)]
    assert db.execute("SELECT thread_id FROM thread_goals").fetchall() == [(keep,)]
    assert db.execute("SELECT * FROM thread_spawn_edges").fetchall() == [(keep, keep)]
    assert db.execute("SELECT id FROM other").fetchall() == [(gone,)]  # not a thread table
    db.close()
    for unsafe in ("", "a", "x" * 10, "../" + "a" * 20, "a b" * 10):
        with pytest.raises(ValueError):
            erase(str(tmp_path), unsafe)
    with pytest.raises(ValueError):
        cli_domains.erase_command("claude", _standard("claude"), "k-1", "short")


def test_app_volumes_never_include_web_state_or_the_shared_login():
    backends = {name: _standard(name) for name in ("claude", "codex", "cursor")}
    assert sorted(cli_domains.app_volumes(backends, "k-1")) == [
        "harness-cli-claude-app-k-1", "harness-cli-codex-app-k-1", "harness-cli-cursor-app-k-1"]
    with pytest.raises(ValueError):
        cli_domains.app_volumes(backends, "")


def test_codex_is_unavailable_to_an_app_until_its_own_login_exists(tmp_path, monkeypatch):
    from harness.manager import Manager
    from test_daemon import make_cfg
    logins: set[str] = set()
    calls = []

    def status(name, _cfg, app_id=""):
        calls.append((name, app_id))
        return app_id in logins if app_id else True
    monkeypatch.setattr(backend_state, "_subscription_status", status)
    cfg = make_cfg(tmp_path)
    cfg.backends["codex"] = BackendConfig(enabled=True, volume="harness-auth-codex", model="gpt-5.6-sol")
    cfg.backends["claude"] = BackendConfig(enabled=True)
    m = Manager(cfg)
    assert backend_state.view(m, "codex")["available"]
    view = backend_state.view(m, "codex", app_id="k-1")
    assert (view["available"], view["logged_in"]) == (False, False)
    assert ("codex", "k-1") in calls
    # Claude's login is shared, so an App is checked against the Web login.
    assert backend_state.view(m, "claude", app_id="k-1")["available"]
    assert ("claude", "") in calls and ("claude", "k-1") not in calls
    logins.add("k-1")
    assert backend_state.view(m, "codex", app_id="k-1")["available"]


def test_codex_app_session_refuses_to_start_without_its_login(monkeypatch):
    prepared = []

    async def prepare(name, _cfg, app_id=""):
        prepared.append((name, app_id))
    monkeypatch.setattr(cli_domains, "prepare", prepare)
    monkeypatch.setattr(backend_state, "_auth_cache", {})
    logged_in = {"k-1": False}
    probes = []

    def probe(name, _cfg, app_id=""):
        probes.append((name, app_id))
        return logged_in.get(app_id, False)
    monkeypatch.setattr(backend_state, "_probe_subscription", probe)
    cfg = _standard("codex")
    assert not backend_state.subscription_status("codex", cfg, "k-1")  # GET /backends caches "not logged in"
    with pytest.raises(CliBackendError, match=r"login.ps1 codex -App k-1"):
        asyncio.run(ready_domain("codex", cfg, "k-1", ""))
    logged_in["k-1"] = True  # the owner runs login.ps1 codex -App k-1 within the cache's TTL
    asyncio.run(ready_domain("codex", cfg, "k-1", ""))
    asyncio.run(ready_domain("codex", cfg, "k-1", ""))  # a login, once seen, comes from the cache
    assert probes == [("codex", "k-1")] * 3
    asyncio.run(ready_domain("codex", cfg, "k-1", "an-api-key"))  # an API key needs no subscription login
    asyncio.run(ready_domain("codex", cfg, "", ""))               # Web's login is the owner's
    asyncio.run(ready_domain("claude", _standard("claude"), "k-1", ""))  # shared login
    assert prepared == [("codex", "k-1")] * 4 + [("codex", ""), ("claude", "k-1")]


def test_erase_session_and_erase_app_remove_cli_history_and_volumes(tmp_path, monkeypatch):
    from harness.manager import Manager
    from test_daemon import make_cfg
    erased, dropped = [], []

    async def erase_history(backend, _cfg, app_id, conversation):
        erased.append((backend, app_id, conversation))

    async def drop_app_volumes(backends, app_id):
        dropped.append((sorted(backends), app_id))
    monkeypatch.setattr(cli_domains, "erase_history", erase_history)
    monkeypatch.setattr(cli_domains, "drop_app_volumes", drop_app_volumes)
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True)
    m = Manager(cfg)

    async def body():
        await m._erase_cli_history({"backend": "claude", "app_id": "k-1",
                                    "run": {"backend_session_id": "conv-1234567890abcdef"}})
        await m._erase_cli_history({"backend": "local", "app_id": "k-1", "run": {}})
        await m._erase_cli_history({"backend": "claude", "app_id": "", "run": {}})
        await m.erase_app("k-1")
    asyncio.run(body())
    assert erased == [("claude", "k-1", "conv-1234567890abcdef")]
    assert dropped == [(["claude"], "k-1")]


# Container level: fake CLIs in the real image, with the mounts the sessions use.
IMAGE = "agent-harness-cli:1"
docker_ok = bool(shutil.which("docker")) and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
needs_docker = pytest.mark.skipif(not docker_ok, reason=f"needs Docker and the {IMAGE} image")


def _fake_cli(backend: str, cfg: BackendConfig, app_id: str, workspace: Path, script: str) -> str:
    session = SESSIONS[backend](session_id=f"t371-{uuid.uuid4().hex[:8]}", workspace=workspace, backend=cfg,
                                sandbox=SandboxConfig(), system_prompt="system", app_id=app_id)
    command = session.command("go") if backend == "cursor" else session.command()
    command = command[:command.index(cfg.image) + 1] + ["sh", "-c", script]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _remove_volumes(*names: str) -> None:
    subprocess.run(["docker", "volume", "rm", "-f", *names], capture_output=True)


@needs_docker
@pytest.mark.parametrize("backend", ["claude", "codex", "cursor"])
def test_domains_cannot_read_each_others_history_or_plant_config(tmp_path, backend):
    cfg = BackendConfig(enabled=True, image=IMAGE, network="none", volume=f"t371-{uuid.uuid4().hex[:10]}")
    layout = cli_domains.LAYOUTS[backend]
    domains = {"web": "", "a": "app-a", "b": "app-b"}
    volumes = {cli_domains.state_volume(backend, cfg, a) for a in domains.values()}
    volumes |= {cli_domains.login_volume(backend, cfg, a) for a in domains.values()}
    try:
        for name, app_id in domains.items():
            subprocess.run(cli_domains.prepare_command(backend, cfg, app_id), check=True, capture_output=True)
            _fake_cli(backend, cfg, app_id, tmp_path, f"mkdir -p {layout.state_dir}/projects && "
                      f"echo HISTORY-{name} > {layout.state_dir}/projects/history-{name}.jsonl")
        targets = [f"{layout.state_dir}/{f}" for f, _ in layout.ro_files]
        targets += [f"{layout.state_dir}/{d}/planted" for d in layout.ro_dirs]
        plant = " ; ".join(f"(echo PLANTED >> {t}) 2>/dev/null && echo WROTE {t}" for t in targets)
        out = _fake_cli(backend, cfg, "app-a", tmp_path, f"{plant} ; grep -rsl --exclude-dir=.local --exclude-dir=.cache HISTORY- /home/agent /tmp ; true")
        assert "WROTE" not in out, out
        found = [line for line in out.splitlines() if line.strip()]
        assert found == [f"{layout.state_dir}/projects/history-a.jsonl"], out
        for name, app_id in domains.items():
            seen = _fake_cli(backend, cfg, app_id, tmp_path,
                             "grep -rsh --exclude-dir=.local --exclude-dir=.cache -e HISTORY- -e PLANTED /home/agent")
            assert seen.split() == [f"HISTORY-{name}"], (name, seen)
    finally:
        _remove_volumes(*volumes)


@needs_docker
def test_erase_and_app_drop_reach_the_volumes(tmp_path):
    cfg = BackendConfig(enabled=True, image=IMAGE, network="none", volume=f"t371-{uuid.uuid4().hex[:10]}")
    backends = {"claude": cfg}
    keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
    state = cli_domains.state_volume("claude", cfg, "app-a")
    login = cli_domains.login_volume("claude", cfg, "app-a")
    try:
        subprocess.run(cli_domains.prepare_command("claude", cfg, "app-a"), check=True, capture_output=True)
        write = " && ".join(f"mkdir -p /home/agent/.claude/projects/-workspace/{c} && "
                            f"echo x > /home/agent/.claude/projects/-workspace/{c}.jsonl" for c in (keep, gone))
        _fake_cli("claude", cfg, "app-a", tmp_path, write + f" && echo '{{\"sessionId\":\"{gone}\"}}' "
                  f"> /home/agent/.claude/history.jsonl")
        asyncio.run(cli_domains.erase_history("claude", cfg, "app-a", gone))
        listing = _fake_cli("claude", cfg, "app-a", tmp_path,
                            "find /home/agent/.claude/projects; cat /home/agent/.claude/history.jsonl; "
                            "ls -ln /home/agent/.claude/history.jsonl")
        assert gone not in listing and keep in listing
        assert " 1000 1000 " in listing  # rewritten in place: still the agent's
        asyncio.run(cli_domains.drop_app_volumes(backends, "app-a"))
        assert subprocess.run(["docker", "volume", "inspect", state], capture_output=True).returncode != 0
        assert subprocess.run(["docker", "volume", "inspect", login], capture_output=True).returncode == 0
        # An App that never ran a hosted CLI has no volumes; dropping them still succeeds.
        asyncio.run(cli_domains.drop_app_volumes(backends, "app-never-ran"))
    finally:
        _remove_volumes(state, login)
