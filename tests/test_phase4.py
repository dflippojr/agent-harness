"""Phase 4 tests: sessions that run their tools on a runner (the MacBook).

A fake runner drives the real macrunner Executor in-process against temp directories, without sandbox-exec,
using Git Bash as the shell on Windows. Timings of the runner hub are shrunk so offline/online transitions take
fractions of a second.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import remote
from harness.api import create_app
from harness.config import Project, RunnerConfig
from harness.fileops import FileOps, ToolError
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.notify import Notifier
from harness.remote import RunnerError, RunnerHub

from test_daemon import Script, call, events, make_cfg, wait_status
from test_phase3 import make_repo, sh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "macrunner"))
import harness_runner  # noqa: E402


def bash() -> str | None:
    if os.name != "nt":
        return "/bin/bash"
    git = shutil.which("git")  # ...\Git\cmd\git.exe or ...\Git\mingw64\bin\git.exe
    for parent in Path(git).resolve().parents if git else []:
        if (parent / "bin" / "bash.exe").exists() and (parent / "git-bash.exe").exists():
            return str(parent / "bin" / "bash.exe")
    return None


@pytest.fixture(autouse=True)
def fast_hub(monkeypatch):
    monkeypatch.setattr(remote, "ONLINE_SECONDS", 0.6)
    monkeypatch.setattr(remote, "POLL_HOLD_SECONDS", 0.15)
    monkeypatch.setattr(remote, "REDELIVER_SECONDS", 0.3)


class FakeRunner:
    """The runner's poll loop, in-process: poll the hub, run each request with the real Executor, post results."""

    def __init__(self, hub: RunnerHub, executor: harness_runner.Executor, name: str = "macbook"):
        self.hub, self.executor, self.name = hub, executor, name
        self.instance = uuid.uuid4().hex
        self.inflight: set[str] = set()
        self.task: asyncio.Task | None = None
        self.seen_ops: list[str] = []
        self.drop: set[str] = set()  # ops whose first delivery is "lost" (never answered)

    def start(self) -> "FakeRunner":
        self.task = asyncio.create_task(self._loop())
        return self

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _loop(self) -> None:
        while True:
            resp = await self.hub.poll(self.name, self.instance, list(self.inflight), self.executor.info())
            for req in resp["requests"]:
                if req["id"] not in self.inflight:
                    self.inflight.add(req["id"])
                    asyncio.create_task(self._work(req))
            await asyncio.sleep(0.01)

    async def _work(self, req: dict) -> None:
        self.seen_ops.append(req["op"])
        if req["op"] in self.drop:
            self.drop.discard(req["op"])
            self.inflight.discard(req["id"])  # forgotten: the hub must redeliver it
            return
        try:
            value = await asyncio.to_thread(self.executor.handle, req["id"], req["op"], req["params"])
            self.hub.result(self.name, req["id"], True, value)
        except harness_runner.OpError as e:
            self.hub.result(self.name, req["id"], False, error=str(e), kind=e.kind)
        finally:
            self.inflight.discard(req["id"])


def mac_cfg(tmp: Path, repo: str = ""):
    cfg = make_cfg(tmp)
    token = tmp / "runner.token"
    token.write_text("s3cret-token\n")
    cfg.runners["macbook"] = RunnerConfig(name="macbook", token_file=str(token), min_free_gb=0)
    cfg.projects["mac"] = Project(name="mac", target="macbook", repo=repo)
    cfg.projects["mac-scratch"] = Project(name="mac-scratch", target="macbook")
    return cfg


def executor(tmp: Path, roots: list[Path]) -> harness_runner.Executor:
    return harness_runner.Executor(workspaces=tmp / "mac-workspaces", repo_roots=[str(r) for r in roots],
                                   profile=None, home=tmp, shell=bash() or "bash", min_free_gb=0)


async def settle(m: Manager, sid: str) -> dict:
    await wait_status(m, sid, "done", "failed", "cancelled", timeout=30)
    await asyncio.gather(*list(m.tasks.values()), return_exceptions=True)
    return m.db.get_session(sid)


needs_bash = pytest.mark.skipif(bash() is None, reason="needs bash (Git Bash on Windows)")


@needs_bash
def test_mac_session_edits_runs_saves_and_merges(tmp_path):
    projects_dir = tmp_path / "Projects"
    src = make_repo(projects_dir / "app")
    cfg = mac_cfg(tmp_path, repo=str(src))
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="app.py", content="VALUE = 2\n"),
                               call("run_shell", 1, command="cat app.py && echo from-mac")]),
        Completion(tool_calls=[call("search", 2, pattern="VALUE"), call("read_file", 3, path="app.py")]),
        Completion(content="Changed VALUE on the Mac."),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        runner = FakeRunner(m.hub, executor(tmp_path, [projects_dir])).start()
        await m.start(maintenance=False)
        s = m.create("bump the value", project="mac")
        assert s["target"] == "macbook" and s["workspace"].startswith("macbook:")
        assert "MacBook" in s["context"][0]["content"] and "separate clone" in s["context"][0]["content"]
        s = await settle(m, s["id"])
        sid = s["id"]
        assert s["status"] == "done", s["stop_reason"]
        results = events(m, sid, "tool_result")
        assert "exit code 0" in results[1]["output"] and "from-mac" in results[1]["output"]
        assert "app.py:1: VALUE = 2" in results[2]["output"]
        saved = events(m, sid, "branch_saved")[-1]
        assert saved["auto_commit"] and saved["published"]
        assert sh(src, "show", f"agent/{sid}:app.py") == "VALUE = 2"
        assert (src / "app.py").read_text() == "VALUE = 1\n"  # the user's checkout is untouched
        ws = tmp_path / "mac-workspaces" / sid
        assert (ws / ".git" / "objects" / "info" / "alternates").exists()  # shared clone, no object copy
        diff = (await m.changes(sid))["repos"][0]
        assert "+VALUE = 2" in diff["diff"]
        assert m.summary(s)["target_online"] is True

        s = await m.review(sid, "merge")
        assert s["review"] == "merged" and (src / "app.py").read_text() == "VALUE = 2\n"
        await runner.stop()
        await m.stop()
    asyncio.run(body())


@needs_bash
def test_waits_for_offline_mac_then_resumes(tmp_path):
    cfg = mac_cfg(tmp_path)
    cfg.notify.enabled = True
    script = Script([Completion(tool_calls=[call("run_shell", 0, command="echo hi")]), Completion(content="ok")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start(maintenance=False)
        s = m.create("say hi", project="mac-scratch")
        s = await wait_status(m, s["id"], "waiting_target", timeout=5)
        assert m.scheduler.holder is None  # waiting doesn't hold the GPU
        waiting = [e for e in m.db.events(s["id"]) if e["type"] == "target_waiting"]
        note = Notifier(cfg, m.db).build(waiting[0])
        assert note["title"].startswith("Waiting for the macbook") and note["sequence_id"] == f"target-{s['id']}"
        with pytest.raises(HarnessError) as e:
            await m.changes(s["id"])
        assert e.value.status == 503

        runner = FakeRunner(m.hub, executor(tmp_path, [tmp_path])).start()
        s = await settle(m, s["id"])
        assert s["status"] == "done" and s["answer"] == "ok"
        assert events(m, s["id"], "target_online")
        assert "exit code 0\nhi" in events(m, s["id"], "tool_result")[0]["output"]
        await runner.stop()
        await m.stop()
    asyncio.run(body())


def test_project_decides_target(tmp_path):
    cfg = mac_cfg(tmp_path)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="hi")]))
        with pytest.raises(HarnessError, match="runs on the tower"):
            m.create("x", project="scratch", target="macbook")
        with pytest.raises(HarnessError, match="runs on the macbook"):
            m.create("x", project="mac-scratch", target="tower")
        m.hub.state["macbook"].info = {"free_gb": 3}
        cfg.runners["macbook"].min_free_gb = 10
        with pytest.raises(HarnessError) as e:
            m.create("x", project="mac-scratch")
        assert e.value.status == 507
        await m.stop()
    asyncio.run(body())


# hub semantics
def test_hub_redelivers_lost_requests_and_fails_on_restart(tmp_path):
    hub = RunnerHub({"macbook": RunnerConfig(name="macbook")})

    async def body():
        call_task = asyncio.create_task(hub.call("macbook", "size", {"session": "0123456789"}, timeout=30))
        first = await hub.poll("macbook", "inst-1", [], {})
        assert [r["op"] for r in first["requests"]] == ["size"]
        rid = first["requests"][0]["id"]
        # still in flight: not resent
        await asyncio.sleep(0.35)
        assert (await hub.poll("macbook", "inst-1", [rid], {}))["requests"] == []
        # the runner doesn't know it (lost response): resent
        again = await hub.poll("macbook", "inst-1", [], {})
        assert [r["id"] for r in again["requests"]] == [rid]
        # the runner restarts before answering: the call fails with "effects unknown"
        await hub.poll("macbook", "inst-2", [], {})
        with pytest.raises(RunnerError) as e:
            await call_task
        assert e.value.kind == "restarted"
        assert not hub.result("macbook", rid, True, 1)  # a late answer from the old instance is ignored
    asyncio.run(body())


def test_hub_timeout_ignores_offline_time(tmp_path, monkeypatch):
    hub = RunnerHub({"macbook": RunnerConfig(name="macbook")})

    async def body():
        await hub.poll("macbook", "i", [], {})
        task = asyncio.create_task(hub.call("macbook", "shell", {}, timeout=1.0))
        await hub.poll("macbook", "i", [], {})  # delivered
        await asyncio.sleep(4.5)  # offline after 0.6 s: most of this doesn't count
        assert not task.done()
        async def poll_until_done():
            for _ in range(6):  # online again: the remaining budget runs out
                await hub.poll("macbook", "i", [], {})
                await asyncio.sleep(0.4)
                if task.done():
                    break
            await task

        with pytest.raises(RunnerError) as e:
            await poll_until_done()
        assert e.value.kind == "timeout"
        cancel = await hub.poll("macbook", "i", [], {})
        assert [r["op"] for r in cancel["requests"]] == ["cancel"]
    asyncio.run(body())


def test_runner_endpoints_need_the_token(tmp_path):
    cfg = mac_cfg(tmp_path)
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        body = {"instance": "abc", "inflight": [], "info": {"free_gb": 42}}
        assert client.post("/runners/macbook/poll", json=body).status_code == 401
        assert client.post("/runners/macbook/poll", json=body,
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/runners/nope/poll", json=body,
                           headers={"Authorization": "Bearer s3cret-token"}).status_code == 404
        ok = client.post("/runners/macbook/poll", json=body, headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200 and ok.json() == {"requests": [], "keep_awake": False}
        status = client.get("/runners").json()
        assert status[0]["name"] == "macbook" and status[0]["online"] and status[0]["info"]["free_gb"] == 42
        projects = {p["name"]: p["target"] for p in client.get("/projects").json()}
        assert projects["mac"] == "macbook" and projects["scratch"] == "tower"


# runner executor
def test_runner_info_includes_disk_total(tmp_path):
    ex = executor(tmp_path, [tmp_path / "Projects"])
    info = ex.info()
    assert info["free_gb"] >= 0
    assert info["total_gb"] >= info["free_gb"]


def test_executor_refuses_repos_outside_roots_and_bad_ids(tmp_path):
    ex = executor(tmp_path, [tmp_path / "Projects"])
    with pytest.raises(harness_runner.OpError, match="allowed project directory"):
        ex.handle("r", "prepare", {"session": "0123456789", "repo": str(tmp_path / "elsewhere")})
    with pytest.raises(harness_runner.OpError, match="bad session id"):
        ex.handle("r", "file", {"session": "../../etc", "name": "list_files", "args": {}, "context_tokens": 1000})
    with pytest.raises(harness_runner.OpError) as e:
        ex.handle("r", "file", {"session": "0123456789", "name": "read_file", "args": {"path": "../x"},
                                "context_tokens": 1000})
    assert e.value.kind == "tool"


@needs_bash
def test_executor_shell_timeout_and_absolute_workspace_paths(tmp_path):
    ex = executor(tmp_path, [tmp_path])
    sid = "0123456789"
    out = ex.handle("r1", "shell", {"session": sid, "command": "sleep 5", "timeout": 1})
    assert out["code"] == 124 and "timed out" in out["output"]
    ws = ex.workspace(sid)
    ex.handle("r2", "file", {"session": sid, "name": "write_file",
                             "args": {"path": f"{ws}/sub/a.txt", "content": "x"}, "context_tokens": 1000})
    assert (ws / "sub" / "a.txt").read_text() == "x"


def test_executor_gives_each_session_a_private_tmpdir(tmp_path, monkeypatch):
    envs = []

    class FakeProc:
        pid, returncode = 1, 0

        def __init__(self, argv, env, **_kw):
            envs.append(env)
            self.stdout = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    monkeypatch.setattr(harness_runner.subprocess, "Popen", FakeProc)
    ex = executor(tmp_path, [tmp_path])
    a, b = "0123456789", "abcdef0123"
    for sid in (a, a, b):
        assert ex.handle("r", "shell", {"session": sid, "command": "true"})["code"] == 0
    first, again, other = (Path(env["TMPDIR"]) for env in envs)
    assert first == again and first != other
    for private in (first, other):
        assert private.is_dir() and private.parent == shared
        if os.name == "posix":
            assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert ex.handle("r", "cleanup_workspace", {"session": a}) == {"removed": True}
    assert not first.exists() and other.is_dir()
    ex.handle("r", "shell", {"session": a, "command": "true"})
    assert Path(envs[-1]["TMPDIR"]).is_dir() and Path(envs[-1]["TMPDIR"]) != first


def test_executor_put_file_writes_png_and_refuses_escape(tmp_path, monkeypatch):
    import base64

    import harness.fileops as fileops

    monkeypatch.setattr(fileops, "MAX_PUT_BYTES", 16)
    ex = executor(tmp_path, [tmp_path])
    sid = "0123456789"
    png = b"\x89PNG\r\n\x1a\nfake"
    out = ex.handle("r", "put_file", {
        "session": sid, "path": "assets/icon.png", "content_b64": base64.b64encode(png).decode(),
    })
    assert "assets/icon.png" in out
    assert (ex.workspace(sid) / "assets" / "icon.png").read_bytes() == png
    png_b64 = base64.b64encode(png).decode()
    with pytest.raises(harness_runner.OpError, match="escapes"):
        ex.handle("r", "put_file", {"session": sid, "path": "../escape.png", "content_b64": png_b64})
    with pytest.raises(harness_runner.OpError, match="base64"):
        ex.handle("r", "put_file", {"session": sid, "path": "x.png", "content_b64": "%%%"})
    with pytest.raises(harness_runner.OpError, match="required"):
        ex.handle("r", "put_file", {"session": sid, "path": "x.png", "content_b64": ""})
    huge = base64.b64encode(b"x" * 17).decode()
    with pytest.raises(harness_runner.OpError, match="limit"):
        ex.handle("r", "put_file", {"session": sid, "path": "big.png", "content_b64": huge})


def test_fileops_skip_symlinks_out_of_the_workspace(tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key.txt").write_text("TOKEN=abc")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("TOKEN=fine")
    try:
        os.symlink(outside, ws / "link", target_is_directory=True)
        os.symlink(outside / "key.txt", ws / "key-link.txt")
    except OSError:
        pytest.skip("creating symlinks needs developer mode on Windows")
    files = FileOps(ws, 10000)
    assert files.search("TOKEN") == "ok.txt:1: TOKEN=fine"
    assert "link" not in files.list_files()
    with pytest.raises(ToolError, match="escapes"):
        files.read_file("key-link.txt")


def test_github_token_patterns_keep_unicode_word_boundaries():
    from harness.skill_validate import SECRET_RES
    patterns = dict(SECRET_RES)
    token = "ghp_" + "A" * 24
    assert patterns["github-token"].search(f" {token} ")
    assert not patterns["github-token"].search("xé" + token)
    assert not patterns["github-token"].search(token + "é")
    pat = "github_pat_" + "B" * 24
    assert patterns["github-pat"].search(f" {pat} ")
    assert not patterns["github-pat"].search("xé" + pat)
