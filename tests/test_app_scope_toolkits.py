"""App sessions get the memory library and homelab only when the owner grants the scope, and an App session's
repository view and erase stay within its own branch."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from harness import projects
from harness.config import Project
from harness.manager import Manager
from harness_modules.homelab.runtime import HOMELAB_PROMPT
from harness_modules.memory_library.runtime import MEMORY_PROMPT

from test_admin import bearer
from test_config_registry import _client, _Toolkit
from test_phase3 import edit_steps, finished, make_repo, project_cfg, sh

MEMORY_TOOLS = {"memory_index", "memory_search", "memory_read"}
HOMELAB_TOOLS = {"homelab_services", "container_logs", "read_service_config", "prometheus_query",
                 "restart_service", "rebuild_service"}


def _lab(tmp_path):
    client, manager = _client(tmp_path)
    manager.cfg.projects["lab"] = Project(name="lab", homelab=True, web=True, memory_library=True,
                                          session_search=True)
    manager.runner.web = _Toolkit(("web_search", "web_fetch"))
    manager.modules.get("memory_library").service = _Toolkit(tuple(sorted(MEMORY_TOOLS)))
    manager.modules.get("search").toolkit = _Toolkit(("session_search", "session_read")).itself
    return client, manager


def _app(client, scopes):
    return bearer(client.post("/keys", json={"name": "app", "kind": "app", "scopes": scopes}).json()["key"])


def _session_tools(client, manager, h, project="lab"):
    created = client.post("/api/v1/sessions", headers=h, json={"prompt": "hello", "project": project})
    assert created.status_code == 201, created.text
    s = manager.db.get_session(created.json()["id"])
    daemon = {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
    workspace = {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}
    return s["context"][0]["content"], daemon, workspace


def test_sessions_scope_alone_gives_no_memory_or_homelab(tmp_path):
    client, manager = _lab(tmp_path)
    with client:
        h = _app(client, ["sessions", "approvals"])
        prompt, daemon, workspace = _session_tools(client, manager, h)
        assert not daemon & MEMORY_TOOLS
        assert not workspace & HOMELAB_TOOLS
        assert MEMORY_PROMPT not in prompt
        assert HOMELAB_PROMPT not in prompt
        assert {"web_search", "session_search"} <= daemon  # other project tools are unchanged
        scratch_daemon = _session_tools(client, manager, h, project="scratch")[1]
        assert not scratch_daemon & MEMORY_TOOLS
        view = client.get("/api/v1/config", headers=h).json()
        caps = next(item for item in view["settings"] if item["key"] == "app.capabilities")
        assert "memory_library" not in caps["effective"] and "homelab" not in caps["effective"]


def test_app_config_cannot_add_capabilities_beyond_its_scopes(tmp_path):
    client, manager = _lab(tmp_path)
    with client:
        h = _app(client, ["sessions"])
        for caps in (["memory_library"], ["homelab"], ["web", "memory_library"]):
            refused = client.patch("/api/v1/config", headers=h, json={"revision": 0,
                                                                      "changes": {"app.capabilities": caps}})
            assert refused.status_code >= 400, refused.text
        narrowed = client.patch("/api/v1/config", headers=h, json={"revision": 0,
                                                                   "changes": {"app.capabilities": []}})
        assert narrowed.status_code == 200, narrowed.text
        reset = client.patch("/api/v1/config", headers=h, json={"revision": narrowed.json()["revision"],
                                                                "changes": {"app.capabilities": None}})
        assert reset.status_code == 200, reset.text
        daemon, workspace = _session_tools(client, manager, h)[1:]
        assert not daemon & MEMORY_TOOLS
        assert not workspace & HOMELAB_TOOLS


def test_owner_granted_scopes_enable_memory_and_homelab_and_the_app_can_narrow(tmp_path):
    client, manager = _lab(tmp_path)
    with client:
        h = _app(client, ["sessions", "memory_library", "homelab"])
        prompt, daemon, workspace = _session_tools(client, manager, h)
        assert MEMORY_TOOLS <= daemon
        assert "restart_service" in workspace and "container_logs" in workspace
        assert HOMELAB_PROMPT in prompt
        narrowed = client.patch("/api/v1/config", headers=h, json={"revision": 0,
                                                                   "changes": {"app.capabilities": ["web"]}})
        assert narrowed.status_code == 200, narrowed.text
        daemon, workspace = _session_tools(client, manager, h)[1:]
        assert not daemon & MEMORY_TOOLS
        assert not workspace & HOMELAB_TOOLS


def test_owner_sessions_keep_memory_and_homelab(tmp_path):
    client, manager = _lab(tmp_path)
    with client:
        created = client.post("/sessions", json={"prompt": "owner hello", "project": "lab"})
        assert created.status_code == 201, created.text
        s = manager.db.get_session(created.json()["id"])
        assert MEMORY_TOOLS <= {name for kit in manager.runner.daemon_toolkits(s) for name in kit.tool_names}
        assert "restart_service" in {t["function"]["name"] for t in manager.runner.workspace(s).schemas()}


# ---------- repository view ----------
def _source_with_session_branch(tmp_path) -> tuple[Path, str]:
    src = make_repo(tmp_path / "src")
    sh(src, "checkout", "-q", "-b", "agent/other")
    (src / "other.txt").write_text("another session's work\n")
    sh(src, "add", ".")
    sh(src, "commit", "-qm", "other session")
    other = sh(src, "rev-parse", "HEAD")
    sh(src, "checkout", "-q", "main")
    return src, other


def _has_object(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha], capture_output=True).returncode == 0


def test_base_only_clone_holds_just_the_base_branch(tmp_path):
    src, other = _source_with_session_branch(tmp_path)
    project = Project(name="proj", repo=str(src))
    ws = tmp_path / "ws"
    info = projects.prepare(project, ws, "s1", base_only=True)
    assert info["base_branch"] == "main"
    remotes = sh(ws, "for-each-ref", "--format=%(refname)", "refs/remotes").splitlines()
    assert set(remotes) <= {"refs/remotes/origin/main", "refs/remotes/origin/HEAD"}
    assert not _has_object(ws, other)
    # A workspace config asking for every branch doesn't widen the base-only refresh.
    sh(ws, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    assert projects.refresh_origin(ws, "main") == ""
    assert "agent/other" not in sh(ws, "for-each-ref", "--format=%(refname)")
    assert not _has_object(ws, other)


def test_default_clone_still_sees_every_branch(tmp_path):
    src, other = _source_with_session_branch(tmp_path)
    ws = tmp_path / "ws"
    projects.prepare(Project(name="proj", repo=str(src)), ws, "s1")
    assert "refs/remotes/origin/agent/other" in sh(ws, "for-each-ref", "--format=%(refname)", "refs/remotes")
    assert _has_object(ws, other)


# ---------- erase ----------
APP = {"id": "ha-test", "name": "test app", "kind": "app", "scopes": "sessions", "scope_set": {"sessions"}}


def test_app_session_clone_and_erase_stay_within_its_branch(tmp_path):
    src, other = _source_with_session_branch(tmp_path)
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump the value", project="proj", app=APP)["id"])
        branch = s["branch"]
        assert sh(src, "show", f"{branch}:app.py") == "VALUE = 2"  # published like any session
        ws = Path(s["workspace"])
        assert not _has_object(ws, other)
        assert await m.erase_session(s["id"])
        assert sh(src, "branch", "--list", branch) == ""
        assert sh(src, "branch", "--list", "agent/other") != ""  # other branches are untouched
        await m.stop()
    asyncio.run(body())


def test_owner_session_erase_keeps_its_branch(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump the value", project="proj")["id"])
        assert await m.erase_session(s["id"])
        assert sh(src, "branch", "--list", s["branch"]) != ""
        await m.stop()
    asyncio.run(body())


def test_runner_base_only_prepare_and_refresh_hold_just_the_base_branch(tmp_path):
    from test_phase4 import executor

    src, other = _source_with_session_branch(tmp_path / "Projects")
    ex = executor(tmp_path, [tmp_path / "Projects"])
    sid = "0123456789"
    info = ex.handle("r1", "prepare", {"session": sid, "repo": str(src), "base_branch": "", "base_only": True})
    assert info["base_branch"] == "main"
    ws = ex.workspace(sid)
    assert not _has_object(ws, other)
    sh(ws, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    assert ex.handle("r2", "refresh_origin", {"session": sid, "base_branch": "main"}) == ""
    assert not _has_object(ws, other)


def test_base_only_applies_to_app_sessions_on_local_sources():
    from harness.runner import _app_base_only

    app, owner = {"app_id": "ha-test"}, {"app_id": ""}
    assert _app_base_only(app, Project(name="p", repo="D:/src/repo"))
    assert not _app_base_only(app, Project(name="p", repo="https://github.com/example/repo.git"))
    assert not _app_base_only(owner, Project(name="p", repo="D:/src/repo"))


def test_erasing_an_app_session_on_a_runner_discards_its_branch_there(tmp_path, monkeypatch):
    from harness.manager import HarnessError

    m = Manager(project_cfg(tmp_path, "/Users/me/Projects/repo"))
    calls = []

    async def remote(s, op, params, timeout=300):
        calls.append((s["id"], op, params["branch"]))
        if s["id"] == "asleep":
            raise HarnessError(503, "runner is asleep")
        return {"head": ""}

    monkeypatch.setattr(m, "remote", remote)
    base = {"app_id": "ha-test", "branch": "agent/s1", "target": "mac", "project": "proj",
            "base_branch": "main", "title": "t"}
    asyncio.run(m._erase_remote_app_branch({**base, "id": "s1"}))
    asyncio.run(m._erase_remote_app_branch({**base, "id": "asleep"}))  # a sleeping runner doesn't block the erase
    asyncio.run(m._erase_remote_app_branch({**base, "id": "own", "app_id": ""}))
    asyncio.run(m._erase_remote_app_branch({**base, "id": "tower", "target": "tower"}))
    asyncio.run(m._erase_remote_app_branch({**base, "id": "noproj", "project": "missing"}))
    assert calls == [("s1", "discard", "agent/s1"), ("asleep", "discard", "agent/s1")]


def test_erase_continues_when_the_local_branch_cannot_be_deleted(tmp_path, monkeypatch):
    src = make_repo(tmp_path / "src")
    m = Manager(project_cfg(tmp_path, str(src)))

    def fail(project, branch):
        raise projects.GitError("locked")

    monkeypatch.setattr(projects, "discard", fail)
    m._erase_app_branch({"app_id": "ha-test", "branch": "agent/s1", "project": "proj", "target": "tower"})
    m._erase_app_branch({"app_id": "ha-test", "branch": "agent/s1", "project": "missing", "target": "tower"})


def test_app_session_on_a_runner_asks_for_a_base_only_clone(tmp_path):
    from types import SimpleNamespace
    from harness.runner import Runner

    sent = []

    class Hub:
        async def call(self, target, op, params, timeout=0):
            sent.append(params)
            return {}

    project = Project(name="p", repo="/Users/me/Projects/repo", base_branch="main")
    for app_id in ("ha-test", ""):
        s = {"id": "s1", "target": "mac", "app_id": app_id}
        asyncio.run(Runner._first_prepare(SimpleNamespace(hub=Hub()), s, project, tmp_path, True, False))
    assert [p["base_only"] for p in sent] == [True, False]
