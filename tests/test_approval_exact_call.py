"""An approval applies only to the exact call it was given for: same tool call id, tool and arguments (#1063).

Backends whose call ids come from model output can reuse an id. A reused id with a different tool or different
arguments must be asked again, never run on the earlier approval."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, call, events, make_cfg, wait_status

RULES = [{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "sensitive"},
         {"tool": "edit_file", "path": "secret/*", "action": "ask", "reason": "sensitive"}]
FIRST = call("write_file", 0, path="secret/a.txt", content="x")


def _reuse(name: str, **args) -> dict:
    """A later call that carries the first call's id."""
    return {**call(name, 0, **args), "id": FIRST["id"]}


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
        await wait_status(m, sid, "waiting_approval", "done", "failed")
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
