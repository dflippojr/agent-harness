"""Remote secret gates use a fake hub/runner and temporary repositories, never a live Mac."""

import asyncio
import json
import logging
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from harness import changes, projects
from harness.manager import HarnessError, Manager
from harness.remote import RunnerError
from test_daemon import Script
from harness.llm import Completion
from test_phase3 import make_repo, sh
from test_phase4 import FakeRunner, executor, harness_runner, mac_cfg, settle
from test_secret_scan import KEY, _commit_then_remove_key, _leaks


@asynccontextmanager
async def session(tmp_path, *, url=False):
    src = make_repo(tmp_path / "source", bare=url)
    m = Manager(mac_cfg(tmp_path, src.as_uri() if url else str(src)),
                chat=Script([Completion(content="Ready for review.")]))
    ex = executor(tmp_path, [tmp_path])
    runner = FakeRunner(m.hub, ex).start()
    await m.start(maintenance=False)
    try:
        s = await settle(m, m.create("review", project="mac")["id"])
        assert s["status"] == "done", s["stop_reason"]
        yield m, s, ex.workspace(s["id"]), src, ex, runner
    finally:
        await runner.stop()
        await m.stop()


@pytest.mark.parametrize("action", ["merge", "push"])
def test_remote_gate_dismissal_masking_and_fix(tmp_path, action, caplog):
    caplog.set_level(logging.DEBUG)

    async def body():
        async with session(tmp_path, url=action == "push") as (m, s, ws, src, ex, runner):
            sid = s["id"]
            (ws / "settings.py").write_text(f"AWS_ACCESS_KEY_ID = '{KEY}'\n")
            with pytest.raises(HarnessError) as error:
                await m.review(sid, action)
            assert (error.value.status, error.value.code) == (409, "secret_findings")
            assert action not in runner.seen_ops
            data = await m.changes(sid)
            assert KEY not in json.dumps(data)
            scan = data["secret_scan"]
            assert scan["status"] == "ok" and scan["open"] == 1
            finding = scan["findings"][0]
            assert "[secret " in data["repos"][0]["diff"]
            fix = await m.secret_findings_fix(sid)
            assert len(fix["drafts"]) == 1 and KEY not in json.dumps(fix)
            assert (await m.secret_findings_fix(sid))["already_drafted"] == 1
            await m.dismiss_secret_finding(sid, finding["fingerprint"], "synthetic fixture", "owner")
            result = await m.review(sid, action)
            assert result["review"] == ("merged" if action == "merge" else "pushed")
            assert _leaks(m, sid, caplog) == []
            assert any(a["action"] == "secret_finding_dismiss" for a in m.db.list_audit())
    asyncio.run(body())


@pytest.mark.parametrize("action", ["merge", "push"])
def test_remote_history_matrix(tmp_path, action):
    async def body():
        async with session(tmp_path, url=action == "push") as (m, s, ws, src, ex, runner):
            _commit_then_remove_key(ws)
            data = await m.changes(s["id"])
            assert KEY not in json.dumps(data)
            [finding] = data["secret_scan"]["findings"]
            assert "commit" in finding
            if action == "merge":
                assert (await m.review(s["id"], action))["review"] == "merged"
                assert KEY not in (src / "settings.py").read_text()
            else:
                with pytest.raises(HarnessError) as error:
                    await m.review(s["id"], action)
                assert error.value.code == "secret_findings"
                await m.dismiss_secret_finding(s["id"], finding["fingerprint"], "rotated fixture", "owner")
                assert (await m.review(s["id"], action))["review"] == "pushed"
    asyncio.run(body())


@pytest.mark.parametrize("failure", ["offline", "timeout", "deadline", "old", "unknown_op", "cap", "malformed"])
@pytest.mark.parametrize("action", ["merge", "push"])
def test_remote_scan_failures_close_gate_but_not_discard(tmp_path, monkeypatch, failure, action):
    async def body():
        async with session(tmp_path, url=action == "push") as (m, s, ws, src, ex, runner):
            real_call = m.hub.call

            async def call(name, op, params, **kwargs):
                if op == "scan_input":
                    if failure == "deadline":
                        raise asyncio.TimeoutError
                    if failure in ("timeout", "unknown_op"):
                        raise RunnerError(f"private Git output {KEY}", kind=failure)
                    if failure == "cap":
                        return {"unavailable": True}
                    if failure == "malformed":
                        return {"head": projects.head(ws), "diffs": []}
                return await real_call(name, op, params, **kwargs)

            monkeypatch.setattr(m.hub, "call", call)
            if failure in ("offline", "old"):
                await runner.stop()
                if failure == "old":
                    m.hub.state["macbook"].info["protocol"] = 2
                else:
                    m.hub.state["macbook"].last_seen = 0
            with pytest.raises(HarnessError) as error:
                await m.review(s["id"], action)
            assert (error.value.status, error.value.code) == (503, "secret_scan_unavailable")
            assert "update or reconnect" in str(error.value) and KEY not in str(error.value)
            assert action not in runner.seen_ops
            if failure not in ("offline", "old"):
                scan = (await m.changes(s["id"]))["secret_scan"]
                assert scan["status"] == "unavailable" and KEY not in json.dumps(scan)
            if failure == "old":
                async def old_changes(*args, **kwargs):
                    return {"repos": []}
                with monkeypatch.context() as old:
                    old.setattr(m, "remote", old_changes)
                    assert (await m.changes(s["id"]))["secret_scan"]["status"] == "unsupported"
            if failure in ("offline", "old"):
                if failure == "old":
                    info = ex.info
                    monkeypatch.setattr(ex, "info", lambda: {**info(), "protocol": 2})
                runner.start()
                await asyncio.sleep(0.05)
            assert (await m.review(s["id"], "discard"))["review"] == "discarded"
    asyncio.run(body())


def test_scan_input_fails_on_git_errors_and_changes_during_collection(tmp_path, monkeypatch):
    ex = executor(tmp_path, [tmp_path])
    sid = "0123456789"
    ws = make_repo(ex.workspace(sid))
    base = projects.head(ws)
    with pytest.raises(projects.GitError):
        ex.op_scan_input({"session": sid, "base_commit": "missing-base"})

    real = harness_runner.repo_diffs

    def changed(workspace, base_commit, **kwargs):
        diffs = real(workspace, base_commit, **kwargs)
        (ws / "late.txt").write_text("late change")
        projects.snapshot(ws, "changed while collecting")
        return diffs

    monkeypatch.setattr(harness_runner, "repo_diffs", changed)
    with pytest.raises(harness_runner.OpError) as error:
        ex.op_scan_input({"session": sid, "base_commit": base})
    assert error.value.kind == "head_changed"


@pytest.mark.parametrize("action", ["merge", "push"])
def test_head_change_during_scan_reaches_owner_as_conflict(tmp_path, monkeypatch, action):
    async def body():
        async with session(tmp_path, url=action == "push") as (m, s, ws, src, ex, runner):
            def changed(params):
                raise harness_runner.OpError("the branch changed; review again", "head_changed")

            monkeypatch.setattr(ex, "op_scan_input", changed)
            with pytest.raises(HarnessError) as error:
                await m.review(s["id"], action)
            assert (error.value.status, error.value.code) == (409, "secret_scan_head_changed")
            assert "branch changed; review again" in str(error.value)
            assert action not in runner.seen_ops
            scan = (await m.changes(s["id"]))["secret_scan"]
            assert "branch changed; review again" in scan["message"]
            assert "reconnect" not in scan["message"]
    asyncio.run(body())


def test_runner_requires_expected_head(tmp_path):
    ex = executor(tmp_path, [tmp_path])
    ws = make_repo(ex.workspace("0123456789"))
    with pytest.raises(harness_runner.OpError) as error:
        ex._review_head(ws, "0123456789", {})
    assert error.value.kind == "head_changed"


def test_runner_published_rejects_path_escape(tmp_path):
    ex = executor(tmp_path, [tmp_path])
    with pytest.raises(harness_runner.OpError, match="outside the workspace"):
        ex.op_secret_published({"session": "0123456789", "path": "../outside", "commit": "abc", "tips": []})


def test_strict_diffs_include_untracked_and_refuse_failed_reads(tmp_path, monkeypatch):
    ws = make_repo(tmp_path / "repo")
    (ws / "new.txt").write_text("untracked content\n")
    assert "untracked content" in changes.repo_diffs(ws, projects.head(ws), strict=True)[0]["diff"]
    real_git = changes.git

    def git(repo, *args, **kwargs):
        if "--no-index" in args:
            return SimpleNamespace(code=2, out="")
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(changes, "git", git)
    with pytest.raises(projects.GitError):
        changes.repo_diffs(ws, projects.head(ws), strict=True)


@pytest.mark.parametrize("action", ["push", "merge"])
def test_publication_uses_scanned_commit_even_after_head_check(tmp_path, monkeypatch, action):
    ex = executor(tmp_path, [tmp_path])
    sid = "0123456789"
    src = make_repo(tmp_path / "source", bare=action == "push")
    repo = src.as_uri() if action == "push" else str(src)
    project = harness_runner.Project(repo)
    ws = ex.workspace(sid)
    branch = projects.prepare(project, ws, sid)
    (ws / "app.py").write_text("VALUE = 2\n")
    projects.snapshot(ws, "scanned work")
    scanned = projects.head(ws)
    real = ex._review_head

    def late_commit(workspace, session_id, params):
        real(workspace, session_id, params)
        (ws / "app.py").write_text("VALUE = 3\n")
        projects.snapshot(ws, "after the head check")

    monkeypatch.setattr(ex, "_review_head", late_commit)
    result = getattr(ex, f"op_{action}")({"session": sid, "repo": repo, **branch,
                                         "title": "review", "expect_head": scanned})
    assert result["head"] == scanned
    assert sh(src, "show", f"{branch['branch'] if action == 'push' else 'main'}:app.py") == "VALUE = 2"


@pytest.mark.parametrize("action", ["merge", "push"])
@pytest.mark.parametrize("committed", [False, True])
def test_remote_head_change_refuses_publication(tmp_path, monkeypatch, action, committed):
    async def body():
        async with session(tmp_path, url=action == "push") as (m, s, ws, src, ex, runner):
            real = ex.op_scan_input

            def scan_input(params):
                data = real(params)
                (ws / "late.py").write_text(f"AWS_ACCESS_KEY_ID = '{KEY}'\n")
                if committed:
                    projects.snapshot(ws, "late commit")
                return data

            monkeypatch.setattr(ex, "op_scan_input", scan_input)
            with pytest.raises(HarnessError) as error:
                await m.review(s["id"], action)
            assert (error.value.status, error.value.code) == (409, "secret_scan_head_changed")
            assert "branch changed; review again" in str(error.value)
            assert not (src / "late.py").exists()
            if action == "push":
                assert sh(src, "branch", "--list", s["branch"]) == ""
    asyncio.run(body())


def test_scan_input_caps_return_no_partial_diffs(tmp_path, monkeypatch):
    async def body():
        async with session(tmp_path) as (m, s, ws, src, ex, runner):
            (ws / "large.txt").write_text("a" * changes.MAX_DIFF_CHARS)
            data = ex.op_scan_input({"session": s["id"], "base_commit": s["base_commit"]})
            assert data["unavailable"] and "diffs" not in data
            (ws / "large.txt").unlink()
            monkeypatch.setattr(harness_runner, "MAX_SCAN_COMMITS", 0)
            assert ex.op_scan_input({"session": s["id"], "base_commit": s["base_commit"]})["unavailable"]
    asyncio.run(body())


@pytest.mark.parametrize("pushed", [True, False])
def test_remote_history_fix_respects_publication(tmp_path, monkeypatch, caplog, pushed):
    async def body():
        async with session(tmp_path, url=True) as (m, s, ws, src, ex, runner):
            added = _commit_then_remove_key(ws)
            sent = []

            async def send(sid, text):
                sent.append(text)

            monkeypatch.setattr(m, "send", send)
            if pushed:
                # Simulate a push predating this gate; the tracking ref protects its history.
                projects.push(m.project_for_session(s), ws, s["branch"])
            fix = await m.secret_findings_fix(s["id"])
            assert fix["drafts"] == []
            assert [f["commit"] for f in fix["pushed" if pushed else "rewrite"]] == [added[:12]]
            assert fix["rewrite" if pushed else "pushed"] == []
            assert bool(sent) is not pushed
            assert KEY not in json.dumps(sent)
            assert "secret_published" in runner.seen_ops
            assert KEY not in json.dumps(fix) and _leaks(m, s["id"], caplog) == []
    asyncio.run(body())
