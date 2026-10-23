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


class _FakeWeb:
    """Serves web_fetch without a network, so the fetch is a real tool call whose result taints the session."""
    tool_names = {"web_fetch"}

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "web_fetch", "description": "fetch",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}]

    async def call(self, name, args):
        return "a page that says to curl something"


def test_a_web_fetch_taints_the_session_and_the_taint_survives_a_restart_until_cleared(tmp_path):
    cfg = make_cfg(tmp_path, rules=NET_ALLOW)

    async def body():
        # Control: the rule allows a networked shell in an untainted session. A networked shell can't run without
        # the Docker sandbox (CI and some dev hosts don't have it), so the decision is asked of the policy itself.
        assert Policy(NET_ALLOW).decide("run_shell", {"command": "echo hi", "network": True}).action == ALLOW

        m = Manager(cfg, chat=Script([Completion(tool_calls=[call("web_fetch", 0, url="https://evil.example/p")]),
                                      _shell(), Completion(content="done")]))
        m.runner.web = _FakeWeb()
        await m.start()
        s = m.create("read the page", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")  # the same shell the rule allowed is now asked
        added = events(m, s["id"], "taint_added")
        assert [(e["kind"], e["origin"]) for e in added] == [("web_fetch", "evil.example")]
        assert m.db.get_session(s["id"])["taint"][0]["origin"] == "evil.example"
        req = events(m, s["id"], "approval_requested")[0]
        assert "untrusted content from evil.example" in req["reason"]
        m.decide(s["id"], None, approve=False)
        await wait_status(m, s["id"], "done")
        await m.stop()

        m = Manager(cfg, chat=Script([Completion(content="x")]))  # fresh daemon: taint persisted
        assert m.db.get_session(s["id"])["taint"][0]["origin"] == "evil.example"
        out = m.clear_taint(s["id"])
        assert out["taint"] == []
        assert events(m, s["id"], "taint_cleared")
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

        # an allowed hosted CLI WebFetch request taints the session; a later Bash is asked, never auto-approved
        s2 = m.create("cli")
        await wait_status(m, s2["id"], "done")
        cli = Cli()
        req = {"tool_name": "WebFetch", "input": {"url": "https://other.example/x"}, "tool_use_id": "c1"}
        task = asyncio.create_task(m.runner._authorize_cli(s2["id"], cli, "r1", req))
        while not task.done() and not m.db.pending_approvals(s2["id"]):
            await asyncio.sleep(0.01)
        if not task.done():
            m.decide(s2["id"], None, approve=True)
        await task
        assert cli.answers == ["allow"]
        assert m.db.get_session(s2["id"])["taint"][0]["origin"] == "other.example"
        pending = await m.runner._ask_cli_policy(m.db.get_session(s2["id"]), cli, "r2", {}, "Bash",
                                                 {"command": "ls"}, "c2")
        assert pending["status"] == "pending"
        await m.stop()
    asyncio.run(body())


def test_cli_web_request_taints_only_once_allowed(tmp_path):
    """A hosted CLI WebFetch that is denied (by rule or by the owner) never ran, so it must not taint the session."""
    cfg = make_cfg(tmp_path, rules=[{"tool": "WebFetch", "action": "deny"}])

    class Cli:
        def __init__(self):
            self.answers = []

        async def respond_permission(self, request_id, behavior, args, message=""):
            self.answers.append(behavior)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="x")]))
        await m.start()
        s = m.create("cli", project="guarded")
        await wait_status(m, s["id"], "done")
        cli = Cli()
        req = {"tool_name": "WebFetch", "input": {"url": "https://denied.example/x"}, "tool_use_id": "c1"}
        await m.runner._authorize_cli(s["id"], cli, "r1", req)
        assert cli.answers == ["deny"] and not m.db.get_session(s["id"])["taint"]

        cfg.projects["guarded"].rules[:] = [{"tool": "WebFetch", "action": "ask"}]
        req = {"tool_name": "WebFetch", "input": {"url": "https://refused.example/x"}, "tool_use_id": "c2"}
        task = asyncio.create_task(m.runner._authorize_cli(s["id"], cli, "r2", req))
        await wait_status(m, s["id"], "waiting_approval")
        assert not m.db.get_session(s["id"])["taint"]
        m.decide(s["id"], None, approve=False)
        await task
        assert cli.answers[-1] == "deny" and not m.db.get_session(s["id"])["taint"]

        req = {"tool_name": "WebFetch", "input": {"url": "https://approved.example/x"}, "tool_use_id": "c3"}
        task = asyncio.create_task(m.runner._authorize_cli(s["id"], cli, "r3", req))
        await wait_status(m, s["id"], "waiting_approval")
        m.decide(s["id"], None, approve=True)
        await task
        assert cli.answers[-1] == "allow"
        assert [t["origin"] for t in m.db.get_session(s["id"])["taint"]] == ["approved.example"]

        cfg.projects["guarded"].rules[:] = [{"tool": "WebFetch", "action": "allow"}]
        req = {"tool_name": "WebFetch", "input": {"url": "https://allowed.example/x"}, "tool_use_id": "c4"}
        await m.runner._authorize_cli(s["id"], cli, "r4", req)
        assert cli.answers[-1] == "allow"
        assert m.db.get_session(s["id"])["taint"][-1]["origin"] == "allowed.example"
        await m.stop()
    asyncio.run(body())


def test_a_remote_clone_is_asked_about_once_the_session_is_tainted():
    clone = {"url": "https://github.com/example/repo"}
    assert taint.escalate(Decision(ALLOW), "git_clone", clone, [], set()).action == ALLOW
    assert taint.escalate(Decision(ALLOW), "git_clone", clone, SRC, set()).action == ASK
    assert taint.escalate(Decision(ALLOW), "git_clone", {"url": "local:demo"}, SRC, set()).action == ALLOW


def test_rerun_keeps_the_original_sessions_taint(tmp_path):
    """Issue #528: a rerun replays the original prompt, so it starts with the original's taint, as a fork does."""
    from test_smart_approvals import _approve, _enable_smart
    cfg = _enable_smart(make_cfg(tmp_path, rules=NET_ALLOW), tmp_path, "auto")

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="done"), Completion(content="done")]))
        m.runner.smart.complete = _approve
        await m.start()
        s = m.create("tainted", project="guarded", taint=SRC)
        await wait_status(m, s["id"], "done")
        r = m.rerun(s["id"])
        await wait_status(m, r["id"], "done")
        rerun = m.db.get_session(r["id"])
        assert rerun["taint"] == m.db.get_session(s["id"])["taint"] == SRC
        shell = {"command": "echo hi", "network": True}
        assert m.runner._decide(rerun, "run_shell", shell).action == ASK
        tagged = Decision(ASK, "runs a Claude Code shell command", smart_eligible=True)
        extra = await m.runner._review_ask(rerun, "Bash", {"command": "ls"}, tagged)
        assert extra["status"] == "pending" and m.runner.smart.calls == []
        await m.stop()
    asyncio.run(body())


def test_session_read_taints_from_the_session_it_read_in_the_readers_scope(tmp_path):
    """Issue #791: the taint comes from the session session_read resolved in the reader's scope. A member's prefix
    that is unique among their own sessions but also matches the owner's and an App's still passes the taint on."""
    from test_household import ALICE, create_member, household

    def router(messages):
        if any(m["role"] == "tool" for m in messages):
            return Completion(content="noted")
        return Completion(tool_calls=[call("session_read", 0, session_id="7m")])

    client, m = household(tmp_path, steps=[router])
    with client:
        uid = create_member(client, ALICE, "Alice")["user_id"]
        app, _ = m.db.create_api_key("shop", "sessions", "app")
        now = 1_700_000_000.0
        for sid, owner_id, app_id in (("7mmember01", uid, ""), ("7mowner001", "owner", ""),
                                      ("7mapp00001", "owner", app["id"])):
            m.db.insert_session({"id": sid, "project": "scratch", "target": "tower", "model": "fake",
                                 "title": sid, "status": "done", "workspace": "", "created_at": now,
                                 "updated_at": now, "context": [], "answer": "", "owner_id": owner_id,
                                 "app_id": app_id, "taint": SRC})
            m.db.insert_event(sid, "user_message", {"content": f"notes in {sid}"})
        assert sorted(m.db.find_session_ids("7m", kind="agent")) == ["7mapp00001", "7mmember01", "7mowner001"]

        async def body():
            s = m.create("read my earlier notes", owner_id=uid)
            await wait_status(m, s["id"], "done")
            read = events(m, s["id"], "tool_result")[0]
            assert read["ok"] and "notes in 7mmember01" in read["output"]
            assert [(t["kind"], t["origin"]) for t in m.db.get_session(s["id"])["taint"]] == [
                ("session", "session 7mmember01 (evil.example)")]
        client.portal.call(body)
