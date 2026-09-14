"""Phase 3 tests: git project branches and review, homelab tools, quotas, cleanup. None need Docker or the GPU."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness import homelab as homelab_mod
from harness import maintenance as maintenance_mod
from harness.config import HomelabConfig, HomelabService, Project
from harness.homelab import Homelab, format_prometheus
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.policy import ALLOW, ASK, DENY, Policy
from harness.tools import ToolError

from test_daemon import Script, call, events, make_cfg, wait_status


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, bare: bool = False) -> Path:
    work = path if not bare else path.parent / (path.name + "-seed")
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "app.py").write_text("VALUE = 1\n")
    sh(work, "add", ".")
    sh(work, "commit", "-qm", "init")
    if bare:
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(path)], check=True)
    return path


def project_cfg(tmp: Path, repo: str, **project):
    cfg = make_cfg(tmp)
    cfg.projects["proj"] = Project(name="proj", repo=repo, **project)
    return cfg


def edit_steps(content: str = "VALUE = 2\n") -> Script:
    return Script([
        Completion(tool_calls=[call("write_file", 0, path="app.py", content=content)]),
        Completion(content="Changed VALUE."),
    ])


async def finished(m: Manager, sid: str) -> dict:
    await wait_status(m, sid, "done", "failed")
    await asyncio.gather(*list(m.tasks.values()), return_exceptions=True)  # branch is saved as the run wraps up
    return m.db.get_session(sid)


# git projects
def test_session_branch_saved_and_squash_merged(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = m.create("bump the value", project="proj")
        s = await finished(m, s["id"])
        sid, branch = s["id"], f"agent/{s['id']}"
        assert s["status"] == "done" and s["branch"] == branch and s["base_branch"] == "main"
        assert "`origin/main`" in s["context"][0]["content"] and "{base_branch}" not in s["context"][0]["content"]
        saved = events(m, sid, "branch_saved")[-1]
        assert saved["auto_commit"] and saved["published"] and len(saved["commits"]) == 1
        # the branch is in the source repo; main is untouched
        assert sh(src, "show", f"{branch}:app.py") == "VALUE = 2"
        assert (src / "app.py").read_text() == "VALUE = 1\n"
        diff = (await m.changes(sid))["repos"][0]
        assert diff["branch"] == branch and "+VALUE = 2" in diff["diff"] and len(diff["commits"]) == 1

        s = await m.review(sid, "merge")
        assert s["review"] == "merged"
        assert (src / "app.py").read_text() == "VALUE = 2\n"
        assert sh(src, "log", "-1", "--format=%s") == "bump the value"
        assert sh(src, "branch", "--list", branch) == ""
        assert events(m, sid, "review")[-1]["state"] == "merged"
        await m.stop()
    asyncio.run(body())


def test_merge_refusals_leave_source_clean(tmp_path):
    src = make_repo(tmp_path / "src")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        # someone changes the same line on main meanwhile
        (src / "app.py").write_text("VALUE = 3\n")
        sh(src, "commit", "-qam", "conflicting change")
        with pytest.raises(HarnessError) as e:
            await m.review(s["id"], "merge")
        assert e.value.status == 409 and "conflicts in app.py" in str(e.value)
        assert sh(src, "status", "--porcelain") == "" and (src / "app.py").read_text() == "VALUE = 3\n"
        # on another branch: refused before touching anything
        sh(src, "checkout", "-q", "-b", "other")
        with pytest.raises(HarnessError, match="not main"):
            await m.review(s["id"], "merge")
        await m.stop()
    asyncio.run(body())


def test_bare_source_merge_and_discard(tmp_path):
    src = make_repo(tmp_path / "bare.git", bare=True)
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        await m.review(s["id"], "merge")
        assert sh(src, "show", "main:app.py") == "VALUE = 2"
        assert sh(src, "worktree", "list").count("\n") == 0  # temporary worktree is gone

        m.runner.chat = edit_steps("VALUE = 9\n")
        s2 = await finished(m, m.create("try something", project="proj")["id"])
        assert sh(src, "branch", "--list", s2["branch"]) != ""
        s2 = await m.review(s2["id"], "discard")
        assert s2["review"] == "discarded" and s2["workspace_removed"] == 1
        assert sh(src, "branch", "--list", s2["branch"]) == "" and not Path(s2["workspace"]).exists()
        with pytest.raises(HarnessError) as e:
            await m.send(s2["id"], "continue")
        assert e.value.status == 409
        await m.stop()
    asyncio.run(body())


def test_url_project_push_and_policy(tmp_path):
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, remote.as_uri())  # file:// URL: same code path as https

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("bump", project="proj")["id"])
        assert m.summary(s)["repo_kind"] == "url"
        assert sh(remote, "branch", "--list", s["branch"]) == ""  # URL sources are never written implicitly
        with pytest.raises(HarnessError):
            await m.review(s["id"], "merge")
        s = await m.review(s["id"], "push")
        assert s["review"] == "pushed" and sh(remote, "show", f"{s['branch']}:app.py") == "VALUE = 2"
        await m.stop()
    asyncio.run(body())

    repo_policy = Policy([], repo=True)
    assert repo_policy.decide("run_shell", {"command": "git push origin HEAD"}).action == DENY
    assert repo_policy.decide("run_shell", {"command": "git commit -am x"}).action == ALLOW
    assert Policy().decide("restart_service", {"service": "grafana"}).action == ASK


def test_clone_failure_fails_session_cleanly(tmp_path):
    cfg = project_cfg(tmp_path, str(tmp_path / "missing"))

    async def body():
        m = Manager(cfg, chat=edit_steps())
        await m.start(maintenance=False)
        s = await finished(m, m.create("x", project="proj")["id"])
        assert s["status"] == "failed" and s["stop_reason"].startswith("workspace_error")
        await m.stop()
    asyncio.run(body())


# quotas
def test_quota_stops_growth_but_allows_cleanup(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.cleanup.workspace_quota_mb = 1
    big = "x" * (3 * 2**20)
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="big.bin", content=big),
                               call("list_files", 1)]),
        Completion(tool_calls=[call("write_file", 2, path="big.bin", content="small")]),
        Completion(content="cleaned up"),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start(maintenance=False)
        s = await finished(m, m.create("fill the disk")["id"])
        assert s["status"] == "failed" and s["stop_reason"].startswith("quota_exceeded: 3 MB")
        assert s["context"][-1]["content"].startswith("Not run: the workspace is over")
        await m.send(s["id"], "delete it")
        s = await finished(m, s["id"])
        assert s["status"] == "done" and s["answer"] == "cleaned up"
        await m.stop()
    asyncio.run(body())


# cleanup
def test_cleanup_removes_old_workspaces_keeps_unsaved(tmp_path, monkeypatch):
    async def no_docker(args, timeout=60, input_=None):
        return 0, "", ""
    monkeypatch.setattr(maintenance_mod, "run_cmd", no_docker)
    src = make_repo(tmp_path / "src")
    remote = make_repo(tmp_path / "remote.git", bare=True)
    cfg = project_cfg(tmp_path, str(src))
    cfg.projects["remote"] = Project(name="remote", repo=remote.as_uri())

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="ok")]))
        await m.start(maintenance=False)
        scratch = await finished(m, m.create("hello")["id"])
        m.runner.chat = edit_steps()
        local = await finished(m, m.create("bump", project="proj")["id"])
        m.runner.chat = edit_steps()
        url = await finished(m, m.create("bump", project="remote")["id"])
        # uncommitted edit in the local session after its run: cleanup must save it to the branch first
        (Path(local["workspace"]) / "late.txt").write_text("late\n")
        orphan = cfg.workspaces_dir / "orphan123"
        orphan.mkdir()
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        fresh = await m.maintenance.cleanup()
        assert fresh["workspaces_removed"] == [] and fresh["orphans_removed"] == ["orphan123"]

        report = await m.maintenance.cleanup(now=time.time() + 15 * 86400)
        assert sorted(report["workspaces_removed"]) == sorted([scratch["id"], local["id"]])
        assert report["kept"] == [{"session": url["id"], "reason": "branch was never pushed"}]
        assert sh(src, "show", f"{local['branch']}:late.txt") == "late"
        assert m.db.get_session(local["id"])["workspace_removed"] == 1
        assert (await m.changes(local["id"]))["removed"]
        usage = await m.maintenance.usage()
        assert [w["session"] for w in usage["workspaces"]] == [url["id"]] and usage["free_gb"] > 0
        await m.stop()
    asyncio.run(body())


# homelab
def lab(tmp_path: Path) -> Homelab:
    root = tmp_path / "Docker"
    for rel, text in {"web/docker-compose.yml": "services: {}\n", "web/config/app.yaml": "a: 1\n",
                      "web/secrets/token.txt": "s3cret", "web/data/db.sqlite": "x", "web/.env": "PASSWORD=x",
                      "other/compose.yml": "x"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return Homelab(HomelabConfig(docker_root=str(root), services={"web": HomelabService(name="web", stack="web")}))


def test_homelab_config_reads_are_contained(tmp_path):
    h = lab(tmp_path)
    assert h.read_service_config("web/config/app.yaml") == "a: 1\n"
    assert h.read_service_config(".") == "web/"
    assert h.read_service_config("web") == "config/\ndocker-compose.yml"
    for bad, msg in [("web/secrets/token.txt", "not readable"), ("web/data/db.sqlite", "not readable"),
                     ("web/.env", "not readable"), ("other/compose.yml", "not a managed stack"),
                     ("../outside.txt", "escapes"), ("web/../../x", "escapes")]:
        with pytest.raises(ToolError, match=msg):
            h.read_service_config(bad)


def test_homelab_docker_calls(tmp_path, monkeypatch):
    h = lab(tmp_path)
    calls = []

    async def fake(args, timeout=60, input_=None):
        calls.append(args)
        if args[:2] == ["docker", "inspect"] and "-f" not in args:
            return 0, ('[{"Name": "/web", "RestartCount": 0, "State": {"Status": "exited", "ExitCode": 137, '
                       '"OOMKilled": true, "StartedAt": "2026-09-14T10:00:00Z", "FinishedAt": "2026-09-14T11:00:00Z"}, '
                       '"HostConfig": {"RestartPolicy": {"Name": "always"}}, "Config": {"Image": "web:1", '
                       '"Env": ["SECRET=nope"]}}]'), ""
        if args[:3] == ["docker", "inspect", "-f"]:
            return (1, "", "no such container") if len(calls) == 1 else (0, "running since now", "")
        if args[:2] == ["docker", "logs"]:
            return 0, "2026-09-14T10:00:01Z started\n", "2026-09-14T10:00:02Z boom\n"
        return 0, "", ""
    monkeypatch.setattr(homelab_mod, "run_cmd", fake)

    async def body():
        status = await h.homelab_services()
        assert "exited" in status and "exit code 137" in status and "OOM-killed" in status and "SECRET" not in status
        logs = await h.container_logs("web", tail=5000, since="30m")
        assert logs.splitlines() == ["2026-09-14T10:00:01Z started", "2026-09-14T10:00:02Z boom"]
        assert calls[-1][:5] == ["docker", "logs", "--timestamps", "--tail", "2000"]
        with pytest.raises(ToolError):
            await h.container_logs("web", since="; rm -rf /")
        with pytest.raises(ToolError, match="unknown service"):
            await h.restart_service("portainer")
        calls.clear()
        out = await h.restart_service("web")  # container missing -> compose up
        assert "recreated with docker compose" in out and calls[1][:4] == ["docker", "compose", "--project-directory",
                                                                           str(Path(h.cfg.docker_root) / "web")]
    asyncio.run(body())


def test_homelab_tools_only_in_homelab_projects(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.projects["lab"] = Project(name="lab", homelab=True)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="hi")]))
        s_lab = m.create("x", project="lab")
        s_plain = m.create("y")
        names = lambda s: {t["function"]["name"] for t in m.runner.workspace(m.db.get_session(s["id"])).schemas()}
        assert "restart_service" in names(s_lab) and "restart_service" not in names(s_plain)
        assert "Homelab access" in m.db.get_session(s_lab["id"])["context"][0]["content"]
        await asyncio.gather(*m.tasks.values(), return_exceptions=True)
    asyncio.run(body())


def test_format_prometheus():
    vec = {"resultType": "vector", "result": [{"metric": {"__name__": "up", "job": "grafana"}, "value": [1, "1"]}]}
    assert format_prometheus(vec) == 'up{job="grafana"} 1'
    mat = {"resultType": "matrix", "result": [{"metric": {"job": "x"}, "values": [[1, "2"], [2, "5"], [3, "3"]]}]}
    assert "min 2, max 5, last 3" in format_prometheus(mat)
    assert format_prometheus({"resultType": "vector", "result": []}) == "no data"
