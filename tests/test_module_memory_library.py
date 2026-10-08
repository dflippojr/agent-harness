"""Memory extraction contracts; all library files and repositories are temporary."""
import asyncio

import httpx
import pytest

from harness import cli
from harness.api import create_app
from harness.config import Project
from harness.manager import Manager
from harness.settings_keys import build_registry
from harness_modules.memory_library import settings, service
from test_daemon import make_cfg


def manager(tmp_path, selection="present"):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.memory_library.clone_dir = str(tmp_path / "library")
    cfg.memory_library.enabled = selection == "present"
    if selection == "absent":
        cfg.module_packages = ["harness_modules.images"]
    return Manager(cfg)


@pytest.mark.parametrize("selection", ["present", "off", "absent", "uninstalled"])
def test_presence_routes_settings_tools_and_prompts(tmp_path, selection):
    m = manager(tmp_path, selection)
    if selection == "uninstalled":
        m.db.close()
        m.cfg.installed.memory_library = False
        m = Manager(m.cfg)
    present = selection in ("present", "off")
    registry = build_registry(m.cfg)
    assert ("memory_library.enabled" in registry.specs) == present
    assert ("modules.memory_library" in registry.specs) == present
    assert ("memory_library" in m.cfg.capabilities()["modules"]) == present
    assert (m.memory_library is not None) == (selection == "present")
    project = Project(name="scratch")
    prompt = m._optional_prompts(project, {}, None)
    assert ("User context: memory_index" in prompt) == (selection == "present")
    session = {"id": "s", "kind": "agent", "project": "scratch", "target": "tower", "app_id": ""}
    kits = m.runner.daemon_toolkits(session)
    assert any("memory_index" in kit.tool_names for kit in kits) == (selection == "present")
    if selection == "present":
        assert m.memory_library not in m.runner.daemon_toolkits({**session, "owner_id": "member"})
        assert m.memory_library not in m.runner.daemon_toolkits({**session, "kind": "chat"})
        assert m.memory_library not in m.runner.daemon_toolkits({**session, "kind": "tools_only"})
        rt = m.modules.get("memory_library")
        assert rt.session_prompt(project, {"app.capabilities": []}, None) == ""
        project.memory_library = False
        assert rt.session_prompt(project, {}, None) == ""
        m.cfg.memory_library.writes = True
        assert "approves every change" in rt.session_prompt(Project(name="scratch"), {}, None)
        assert set(service.TOOLS) == {s["function"]["name"] for s in m.memory_library.schemas()}
    else:
        rt = m.modules.get("memory_library")
        if rt:
            rt.start()
    async def check():
        app = create_app(m)
        app.state.manager = m
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            for prefix in ("", "/api/admin/v1"):
                response = await c.get(prefix + "/memory")
                assert response.status_code == (200 if present else 404)
                if present:
                    assert response.json()["enabled"] == (selection == "present")
                response = await c.put(prefix + "/memory/profile", json={"content": "x"})
                assert response.status_code in ((400,) if present else (404, 405))
    asyncio.run(check())
    m.db.close()


def test_cli_and_setting_accessors(tmp_path):
    m = manager(tmp_path, "off")
    spec = build_registry(m.cfg).get("memory_library.enabled")
    assert not spec.getter(m.cfg)
    spec.setter(m.cfg, True)
    assert spec.getter(m.cfg)
    assert spec.enable_check(m.cfg) == []
    assert spec.yaml_path == ("memory_library", "enabled")
    assert spec.apply_mode == "daemon_restart"
    assert settings.check_memory_library(m.cfg) == []
    for args, expected in [(["memory", "show"], ("GET", "/memory")),
                           (["memory", "set-profile", "new text"], ("PUT", "/memory/profile"))]:
        assert cli.admin_request(cli._build_parser().parse_args(args))[:2] == expected
    m.db.close()


def test_profile_route_tool_error(tmp_path, monkeypatch):
    m = manager(tmp_path)
    m.cfg.memory_library.profile_path = "profile.md"
    async def fail(*args):
        raise service.ToolError("write rejected")
    monkeypatch.setattr(m.memory_library, "owner_write", fail)
    async def check():
        app = create_app(m)
        app.state.manager = m
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            response = await c.put("/api/admin/v1/memory/profile", json={"content": "x"})
            assert response.status_code == 400
            assert "write rejected" in response.text
    asyncio.run(check())
    m.db.close()


def test_read_limits_search_errors_and_proposals(tmp_path):
    cfg = make_cfg(tmp_path).memory_library
    cfg.clone_dir = str(tmp_path)
    cfg.categories = ["work"]
    cfg.profile_path = "profile.md"
    cfg.profile_max_chars = 10
    lib = service.MemoryLibrary(cfg)
    assert lib.profile_text() == ""
    (tmp_path / "profile.md").write_text("abcdefghijklmnop", encoding="utf-8")
    assert lib.profile_text() == "abcdefghij\n[... profile cut at its size limit]"
    work = tmp_path / "categories/work"
    work.mkdir(parents=True)
    path = work / "notes.md"
    path.write_text("hello\n" * 105, encoding="utf-8")
    assert "first 100 matches" in lib.memory_search("hello")
    assert lib.memory_search("missing") == "no matches"
    with pytest.raises(service.ToolError, match="bad regular expression"):
        lib.memory_search("[")
    assert "more lines" in lib.memory_read("categories/work/notes.md", end_line=1)
    assert "past the end" in lib.memory_read("categories/work/notes.md", start_line=500)
    path.write_text("x" * service.MAX_READ_CHARS + "\n", encoding="utf-8")
    assert "stopped at line" in lib.memory_read("categories/work/notes.md")
    path.write_text("", encoding="utf-8")
    assert lib.memory_read("categories/work/notes.md") == "(empty file)"
    for args, message in [({"content": "", "summary": "x"}, "no difference"),
                          ({"content": "new"}, "summary is empty"),
                          ({"content": "x" * (service.MAX_FILE_CHARS + 1), "summary": "x"}, "limit")]:
        with pytest.raises(service.ToolError, match=message):
            lib._proposal("memory_write", {"path": "categories/work/notes.md", **args})
    for old, message in [("", "old_text is empty"), ("missing", "appears 0 times")]:
        with pytest.raises(service.ToolError, match=message):
            lib._proposal("memory_edit", {"path": "categories/work/notes.md", "old_text": old})
    lib.refresh_error = "offline"
    assert "may be stale" in lib.memory_index()
    cfg.profile_path = ""
    path.unlink()
    assert "No readable files" in lib.memory_index()
    assert lib.profile_file() is None


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


def _library(tmp_path):
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    import subprocess
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "categories" / "projects").mkdir(parents=True)
    (seed / "categories" / "projects" / "a.md").write_text("# A\n")
    _git(seed, "add", "."), _git(seed, "commit", "-qm", "one"), _git(seed, "push", "-q", "origin", "HEAD:main")
    m = manager(tmp_path)
    cfg = m.cfg.memory_library
    cfg.repo, cfg.clone_dir = str(remote), str(tmp_path / "library")
    lib = service.MemoryLibrary(cfg, db=m.db)
    alerts = []
    lib.alert = lambda *a: alerts.append(a)
    return lib, seed, alerts


def _push_upstream(seed, name="b.md"):
    (seed / "categories" / "projects" / name).write_text("# B\n")
    _git(seed, "add", "."), _git(seed, "commit", "-qm", "two"), _git(seed, "push", "-q", "origin", "HEAD:main")


def test_dirty_untracked_and_diverged_clones_are_distinct_states(tmp_path):
    lib, seed, alerts = _library(tmp_path)

    async def check():
        await lib.refresh(force=True)
        assert lib.refresh_state == "ok" and lib.last_success and not lib.failures
        _push_upstream(seed)
        (lib.root / "categories" / "projects" / "a.md").write_text("# A\nlocal\n")
        (lib.root / "categories" / "projects" / "new file.md").write_text("x")
        for n in range(3):
            await lib.refresh(force=True)
            assert lib.refresh_state == "dirty" and lib.failures == n + 1
        assert sorted(lib.changed_paths) == ["categories/projects/a.md", "categories/projects/new file.md"]
        assert "local changes" in lib.refresh_error
        assert (lib.root / "categories" / "projects" / "a.md").read_text() == "# A\nlocal\n"  # never discarded
        assert len(alerts) == 1  # once per episode
        await lib.refresh(force=True)
        assert len(alerts) == 1
        _git(lib.root, "checkout", "--", "."), (lib.root / "categories" / "projects" / "new file.md").unlink()
        await lib.refresh(force=True)
        assert lib.refresh_state == "ok" and lib.failures == 0 and not lib.changed_paths
        assert (lib.root / "categories" / "projects" / "b.md").is_file()
        # diverged: a local commit plus a new upstream commit
        (lib.root / "categories" / "projects" / "c.md").write_text("c")
        _git(lib.root, "add", "."), _git(lib.root, "commit", "-qm", "local")
        _push_upstream(seed, "d.md")
        await lib.refresh(force=True)
        assert lib.refresh_state == "diverged" and "cannot fast-forward" in lib.refresh_error
    asyncio.run(check())


def test_writes_refuse_a_dirty_clone_and_metrics_report_state(tmp_path):
    lib, seed, _ = _library(tmp_path)

    class Out:
        rows = {}
        def metric(self, name, kind, help_, rows):
            self.rows[name] = rows

    async def check():
        await lib.refresh(force=True)
        (lib.root / "categories" / "projects" / "a.md").write_text("# A\nlocal\n")
        with pytest.raises(service.ToolError, match="uncommitted"):
            await lib._sync_for_write()
        assert "local" in (lib.root / "categories" / "projects" / "a.md").read_text()
        await lib.refresh(force=True)
    asyncio.run(check())
    from harness_modules.memory_library.runtime import MemoryLibraryRuntime
    rt = MemoryLibraryRuntime.__new__(MemoryLibraryRuntime)
    rt.service, out = lib, Out()
    rt.metrics(out, None)
    assert out.rows["harness_memory_library_refresh_ok"] == [({}, 0)]
    assert ({"state": "dirty"}, 1) in out.rows["harness_memory_library_refresh_state"]
    assert out.rows["harness_memory_library_changed_paths"] == [({}, 1)]
