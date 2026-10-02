"""Issue #262: taint-aware approvals after a session reads untrusted content."""
from __future__ import annotations

import asyncio

from harness import taint
from harness.llm import Completion
from harness.manager import Manager
from harness.policy import ALLOW, ASK, DENY, Decision, Policy
from test_daemon import Script, call, events, make_cfg, wait_status

SRC = taint.add([], "web_fetch", "evil.example")
NET_ALLOW = [{"tool": "run_shell", "network": True, "action": "allow"}]


def test_source_for_extracts_host_and_fallbacks():
    assert taint.source_for("web_fetch", {"url": "https://evil.example/a?b=1"}) == ("web_fetch", "evil.example")
    assert taint.source_for("WebFetch", {"url": "nope"}) == ("web_fetch", "a web page")
    assert taint.source_for("web_search", {"query": "x"}) == ("web_search", "search results")
    assert taint.source_for("read_file", {}) is None


def test_add_dedupes_and_caps():
    assert taint.add(SRC, "web_fetch", "evil.example") is None
    lst = []
    for i in range(30):
        lst = taint.add(lst, "web_fetch", f"h{i}")
    assert len(lst) == taint.MAX_SOURCES and lst[-1]["origin"] == "h29"


def test_escalate_only_risky_allows_and_keeps_deny():
    net = {"command": "curl x", "network": True}
    assert taint.escalate(Decision(ALLOW), "run_shell", net, [], set()).action == ALLOW
    out = taint.escalate(Decision(ALLOW), "run_shell", net, SRC, set())
    assert out.action == ASK and "untrusted content from evil.example" in out.reason
    assert taint.escalate(Decision(ALLOW), "run_shell", {"command": "ls"}, SRC, set()).action == ALLOW
    assert taint.escalate(Decision(ALLOW), "run_shell", {"command": "git push"}, SRC, set()).action == ASK
    assert taint.escalate(Decision(ALLOW), "restart_service", {}, SRC, set()).action == ASK
    assert taint.escalate(Decision(ALLOW), "my_app_tool", {}, SRC, {"my_app_tool"}).action == ASK
    assert taint.escalate(Decision(ALLOW), "read_file", {}, SRC, set()).action == ALLOW
    assert taint.escalate(Decision(DENY, "no"), "run_shell", net, SRC, set()).action == DENY
    assert taint.escalate(Decision(ASK, "r", smart_eligible=True), "Bash", {}, SRC, set()).smart_eligible is False


def test_deny_stays_deny_on_repo_project():
    d = Policy([], repo=True).decide("run_shell", {"command": "git push origin x"})
    assert taint.escalate(d, "run_shell", {"command": "git push origin x"}, SRC, set()).action == DENY


def _shell(**kw):
    return Completion(tool_calls=[call("run_shell", 0, command="echo hi", network=True, **kw)])


def test_tainted_session_asks_survives_restart_and_clear_is_evented(tmp_path):
    cfg = make_cfg(tmp_path, rules=NET_ALLOW)

    async def body():
        m = Manager(cfg, chat=Script([_shell(), Completion(content="done")]))
        await m.start()
        clean = m.create("untainted", project="guarded")
        await wait_status(m, clean["id"], "done")  # rule allows it: unchanged behavior
        await m.stop()

        m = Manager(cfg, chat=Script([_shell(), Completion(content="done")]))
        await m.start()
        s = m.create("tainted", project="guarded", taint=SRC)
        await wait_status(m, s["id"], "waiting_approval")
        req = events(m, s["id"], "approval_requested")[0]
        assert "untrusted content from evil.example" in req["reason"]
        m.decide(s["id"], None, approve=False)
        await wait_status(m, s["id"], "done")
        await m.stop()

        m = Manager(cfg, chat=Script([Completion(content="x")]))  # fresh daemon: taint persisted
        assert m.db.get_session(s["id"])["taint"][0]["origin"] == "evil.example"
        assert m.db.get_session(s["id"])["taint"]
        out = m.clear_taint(s["id"])
        assert out["taint"] == []
        assert events(m, s["id"], "taint_cleared")
    asyncio.run(body())


def test_web_fetch_result_taints_the_session(tmp_path):
    cfg = make_cfg(tmp_path, rules=NET_ALLOW)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="x")]))
        await m.start()
        s = m.create("hi")
        await wait_status(m, s["id"], "done")
        m.runner._taint_from_result(m.db.get_session(s["id"]), "web_fetch", {"url": "https://evil.example/p"})
        assert m.db.get_session(s["id"])["taint"][0]["origin"] == "evil.example"
        assert events(m, s["id"], "taint_added")
        await m.stop()
    asyncio.run(body())


def test_tainted_session_is_never_smart_approved_and_cli_web_taints(tmp_path):
    from test_smart_approvals import _approve, _enable_smart
    cfg = _enable_smart(make_cfg(tmp_path), tmp_path, "auto")

    class Cli:
        def __init__(self):
            self.answers = []

        async def respond_permission(self, request_id, behavior, args, message=""):
            self.answers.append(behavior)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="x")]))
        m.runner.smart.complete = _approve
        await m.start()
        s = m.create("hi")
        await wait_status(m, s["id"], "done")
        tagged = Decision(ASK, "runs a Claude Code shell command", smart_eligible=True)
        extra = await m.runner._review_ask(m.db.get_session(s["id"]), "Bash", {"command": "ls"}, tagged)
        assert extra["status"] == "approved" or m.runner.smart.calls  # control: untainted session is reviewed
        m.runner.smart.calls.clear()
        m.runner._add_taint(s["id"], "web_fetch", "evil.example")
        extra = await m.runner._review_ask(m.db.get_session(s["id"]), "Bash", {"command": "ls"}, tagged)
        assert extra["status"] == "pending" and m.runner.smart.calls == []

        # a hosted CLI WebFetch request taints the session; a later Bash is asked, never auto-approved
        s2 = m.create("cli")
        await wait_status(m, s2["id"], "done")
        cli = Cli()
        await m.runner._ask_cli_policy(m.db.get_session(s2["id"]), cli, "r1", {}, "WebFetch",
                                       {"url": "https://other.example/x"}, "c1")
        assert m.db.get_session(s2["id"])["taint"][0]["origin"] == "other.example"
        pending = await m.runner._ask_cli_policy(m.db.get_session(s2["id"]), cli, "r2", {}, "Bash",
                                                 {"command": "ls"}, "c2")
        assert pending["status"] == "pending"
        await m.stop()
    asyncio.run(body())
