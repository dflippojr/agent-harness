"""An approval applies only to the exact call it was given for: same tool call id, tool and arguments (#1063).

Backends whose call ids come from model output can reuse an id. A reused id with a different tool or different
arguments must be asked again, never run on the earlier approval."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness.llm import Completion
from harness.manager import Manager
from harness.policy import ASK, Decision, harness_tool_names
from waits import scaled
from test_daemon import Script, call, events, make_cfg, wait_status

RULES = [{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "sensitive"},
         {"tool": "edit_file", "path": "secret/*", "action": "ask", "reason": "sensitive"}]
FIRST = call("write_file", 0, path="secret/a.txt", content="x")


def _reuse(name: str, **args) -> dict:
    """A later call that carries the first call's id."""
    return {**call(name, 0, **args), "id": FIRST["id"]}


async def _settled(m: Manager, sid: str, first: str, timeout: float = 30) -> None:
    """Until the run asks about another call than `first`, or ends."""
    deadline = asyncio.get_running_loop().time() + scaled(timeout)
    while asyncio.get_running_loop().time() < deadline:
        if any(a["id"] != first for a in m.db.sessions.pending_approvals(sid)):
            return
        if m.db.sessions.get_session(sid)["status"] in ("done", "failed", "cancelled"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the run neither asked again nor ended")


@pytest.mark.parametrize("second", [
    _reuse("write_file", path="secret/a.txt", content="evil"),        # same tool, different arguments
    _reuse("write_file", path="secret/b.txt", content="x"),           # same tool, different path
    _reuse("edit_file", path="secret/a.txt", old_text="x", new_text="evil"),    # different tool
], ids=["args", "path", "tool"])
def test_a_reused_call_id_is_asked_again_and_a_denial_holds(tmp_path, second):
    cfg = make_cfg(tmp_path, rules=RULES)
    script = Script([
        Completion(tool_calls=[FIRST]),
        Completion(tool_calls=[second]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("write secret", project="guarded")
        sid = s["id"]
        await wait_status(m, sid, "waiting_approval")
        m.decide(sid, None, approve=True)
        # The second call reuses the approved id: it must wait on its own approval, not run on the first one.
        first = m.db.sessions.approvals(sid)[0]["id"]
        await _settled(m, sid, first)
        pending = m.db.sessions.pending_approvals(sid)
        assert len(pending) == 1, "a reused call id with a different call was run on the earlier approval"
        assert pending[0]["tool_call_id"] == FIRST["id"]
        assert pending[0]["tool"] == second["function"]["name"]
        m.decide(sid, None, approve=False, note="no")
        s = await wait_status(m, sid, "done")
        ws = Path(s["workspace"])
        assert (ws / "secret" / "a.txt").read_text() == "x"
        assert not (ws / "secret" / "b.txt").exists()
        assert [a["status"] for a in m.db.sessions.approvals(sid)] == ["approved", "denied"]
        results = events(m, sid, "tool_result")
        assert len(results) == 2 and results[0]["ok"] and not results[1]["ok"]
        await m.stop()
    asyncio.run(body())


def test_the_same_call_with_its_id_reused_keeps_its_approval(tmp_path):
    """Id, tool and arguments all equal: it is the approved call (as on a replay), so it is not asked again."""
    cfg = make_cfg(tmp_path, rules=RULES)
    script = Script([
        Completion(tool_calls=[FIRST]),
        Completion(tool_calls=[_reuse("write_file", path="secret/a.txt", content="x")]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("write secret", project="guarded")
        sid = s["id"]
        await wait_status(m, sid, "waiting_approval")
        m.decide(sid, None, approve=True)
        s = await wait_status(m, sid, "done")
        assert [a["status"] for a in m.db.sessions.approvals(sid)] == ["approved"]
        assert (Path(s["workspace"]) / "secret" / "a.txt").read_text() == "x"
        await m.stop()
    asyncio.run(body())


# The lookup both approval paths share (the daemon's tool loop and a hosted CLI's permission requests).
CALL_ID, TOOL, ARGS = "tool-1", "Bash", {"command": "git push origin main", "timeout": 60}


def _session(db, sid: str, backend: str = "claude") -> None:
    db.sessions.insert_session({"id": sid, "title": "t", "project": "p", "target": "tower", "model": "m",
                                "backend": backend, "status": "running", "workspace": "",
                                "created_at": 1.0, "updated_at": 1.0, "context": [], "run": {}})


@pytest.fixture
def manager(tmp_path):
    m = Manager(make_cfg(tmp_path))
    yield m
    m.db.close()


@pytest.mark.parametrize("call_id", [CALL_ID, "tool-2"])
@pytest.mark.parametrize("tool", [TOOL, "Write"])
@pytest.mark.parametrize("args", [ARGS, {"timeout": 60, "command": "git push origin main"},
                                  {"command": "git push origin main"}, {"command": "git push --force", "timeout": 60}])
def test_the_lookup_matches_only_the_exact_call(manager, call_id, tool, args):
    db = manager.db
    _session(db, "s1")
    db.sessions.insert_approval({"id": "a-1", "session_id": "s1", "tool_call_id": CALL_ID, "tool": TOOL,
                                 "args": ARGS, "reason": "asks", "status": "approved"})
    found = db.sessions.approval_for_exact_call("s1", call_id, tool, args)
    exact = call_id == CALL_ID and tool == TOOL and args == ARGS  # key order is not part of the call
    assert (found is not None and found["id"] == "a-1") is exact
    assert db.sessions.approval_for_exact_call("s2", CALL_ID, TOOL, ARGS) is None  # never another session's


def test_the_lookup_picks_the_exact_call_among_rows_sharing_an_id(manager):
    """A reused id leaves several rows under it: each call finds its own, the newest when it was asked twice."""
    db = manager.db
    _session(db, "s1")
    other = {"command": "rm -rf src"}
    for aid, args, status in (("a-1", ARGS, "approved"), ("a-2", other, "denied"), ("a-3", ARGS, "denied")):
        db.sessions.insert_approval({"id": aid, "session_id": "s1", "tool_call_id": CALL_ID, "tool": TOOL,
                                     "args": args, "reason": "asks", "status": status})
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, TOOL, ARGS)["id"] == "a-3"
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, TOOL, other)["id"] == "a-2"
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, TOOL, {"command": "ls"}) is None


class _FakeCli:
    def __init__(self):
        self.answers: list[tuple] = []

    async def respond_permission(self, request_id, behavior, args, message=""):
        self.answers.append((request_id, behavior, args, message))


@pytest.mark.parametrize("reused", [
    {"tool_name": TOOL, "input": {"command": "git push --force origin main"}},
    {"tool_name": "Write", "input": {"file_path": "x", "content": "y"}},
], ids=["args", "tool"])
def test_a_hosted_request_reusing_an_approved_id_is_asked_again(manager, reused):
    """A hosted CLI's permission request with an approved tool_use_id but another call waits on its own approval;
    denied, the CLI hears a denial."""
    runner, db = manager.runner, manager.db
    _session(db, "s1")
    db.sessions.insert_approval({"id": "a-1", "session_id": "s1", "tool_call_id": CALL_ID, "tool": TOOL,
                                 "args": {"command": "git push origin main"}, "reason": "asks",
                                 "status": "approved"})
    runner.policy = lambda s: _AskAll()
    cli = _FakeCli()

    async def body():
        task = asyncio.create_task(runner._authorize_cli("s1", cli, "r1", {**reused, "tool_use_id": CALL_ID}))
        for _ in range(500):
            pending = db.sessions.pending_approvals("s1")
            if pending or task.done():
                break
            await asyncio.sleep(0.01)
        assert not cli.answers, f"answered on the earlier approval: {cli.answers}"
        assert len(pending) == 1 and pending[0]["tool"] == reused["tool_name"]
        assert pending[0]["tool_call_id"] == CALL_ID
        db.sessions.decide_approval(pending[0]["id"], "denied", "no")
        runner.approval_events[pending[0]["id"]].set()
        await asyncio.wait_for(task, scaled(10))
        assert [a[1] for a in cli.answers] == ["deny"]
    asyncio.run(body())


def test_a_hosted_request_repeating_the_approved_call_is_allowed(manager):
    runner, db = manager.runner, manager.db
    _session(db, "s1")
    db.sessions.insert_approval({"id": "a-1", "session_id": "s1", "tool_call_id": CALL_ID, "tool": TOOL,
                                 "args": {"command": "git push origin main"}, "reason": "asks",
                                 "status": "approved"})
    runner.policy = lambda s: _AskAll()
    cli = _FakeCli()
    request = {"tool_name": TOOL, "input": {"command": "git push origin main"}, "tool_use_id": CALL_ID}
    asyncio.run(asyncio.wait_for(runner._authorize_cli("s1", cli, "r1", request), scaled(10)))
    assert [a[1] for a in cli.answers] == ["allow"]
    assert db.sessions.pending_approvals("s1") == []


class _AskAll:
    def decide(self, name, args):
        return Decision(ASK, "asks")

    def fingerprint(self):
        return "ask-all"


@pytest.mark.parametrize("recorded", ["memory_write", "mcp__harness__memory_write"])
def test_a_harness_tool_finds_its_approval_asked_natively_or_over_mcp(manager, recorded):
    """The memory library reads the approval of the call it applies by its bare tool name: the daemon records that
    name, a hosted CLI the MCP one. Either form matches, but only for the same id and arguments."""
    db = manager.db
    _session(db, "s1")
    args = {"path": "notes.md", "content": "hi", "summary": "note"}
    db.sessions.insert_approval({"id": "a-1", "session_id": "s1", "tool_call_id": CALL_ID, "tool": recorded,
                                 "args": args, "reason": "asks", "status": "approved"})
    names = harness_tool_names("memory_write")
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, names, args)["id"] == "a-1"
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, names, {**args, "content": "other"}) is None
    assert db.sessions.approval_for_exact_call("s1", CALL_ID, harness_tool_names("memory_edit"), args) is None
