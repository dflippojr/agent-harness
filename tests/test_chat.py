"""Chat home: durable non-agent conversations, chat-safe tools, and owner/guest isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from harness.api import create_app
from harness.config import GuestAccess, SearchConfig
from harness.db import Database
from harness.fileops import ToolError
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.principal import Principal
from harness.policy import ChatPolicy
from harness.runner import Runner
from harness.search import SessionSearch, search

from test_api import LOGIN, make_client, wait_for
from test_daemon import Script, make_cfg
from test_phase7 import seed

OWNER = {"Tailscale-User-Login": LOGIN}
CHAT_MARKER = "chatsecretq9w2zx"
AGENT_MARKER = "agentsecretl4m7yw"

# Agent-facing session routes that must 404 a chat id (owned_session / app visible_session).
_CHAT_ON_AGENT_ROUTES = (
    ("GET", "/sessions/{id}"),
    ("PATCH", "/sessions/{id}", {"title": "nope"}),
    ("POST", "/sessions/{id}/messages", {"content": "nope"}),
    ("POST", "/sessions/{id}/rerun"),
    ("GET", "/sessions/{id}/changes"),
    ("GET", "/sessions/{id}/review-comments"),
    ("POST", "/sessions/{id}/review/merge"),
    ("GET", "/sessions/{id}/approvals"),
    ("POST", "/sessions/{id}/approvals/pending", {"decision": "deny", "note": ""}),
    ("POST", "/sessions/{id}/cancel"),
    ("GET", "/sessions/{id}/transcript"),
    ("GET", "/sessions/{id}/events", {"follow": "false"}),
    ("GET", "/api/v1/sessions/{id}"),
    ("PATCH", "/api/v1/sessions/{id}", {"title": "nope"}),
    ("POST", "/api/v1/sessions/{id}/messages", {"content": "nope"}),
    ("POST", "/api/v1/sessions/{id}/rerun"),
    ("GET", "/api/v1/sessions/{id}/changes"),
    ("GET", "/api/v1/sessions/{id}/review-comments"),
    ("POST", "/api/v1/sessions/{id}/review/merge"),
    ("GET", "/api/v1/sessions/{id}/approvals"),
    ("POST", "/api/v1/sessions/{id}/approvals/pending", {"decision": "deny", "note": ""}),
    ("POST", "/api/v1/sessions/{id}/cancel"),
    ("GET", "/api/v1/sessions/{id}/transcript"),
    ("GET", "/api/v1/sessions/{id}/events", {"follow": "false"}),
    ("POST", "/api/v1/sessions/{id}/context", {"context": [{"title": "n", "content": "x"}]}),
    ("GET", "/api/v1/sessions/{id}/tool_calls"),
    ("POST", "/api/v1/sessions/{id}/events/ticket"),
)

_AGENT_ON_CHAT_ROUTES = (
    ("GET", "/chats/{id}"),
    ("PATCH", "/chats/{id}", {"title": "nope"}),
    ("POST", "/chats/{id}/messages", {"content": "nope"}),
    ("POST", "/chats/{id}/cancel"),
    ("DELETE", "/chats/{id}"),
    ("GET", "/chats/{id}/events", {"follow": "false"}),
)


def _request(client, method, path, body=None, params=None):
    kw = {}
    if params:
        kw["params"] = params
    if body is not None:
        kw["json"] = body
    return client.request(method, path, **kw)


def test_chat_is_separate_from_agent_sessions(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="Hello!")])
    with client:
        opts = client.get("/chats/options").json()
        assert opts["default_backend"] == "local"
        assert opts["backends"][0]["models"] == ["fake"]
        chat = client.post("/chats", json={"prompt": "What is a monad?\nBe brief."}).json()
        assert chat["kind"] == "chat"
        assert chat["title"] == "What is a monad?"
        assert chat["project"] == "scratch"
        wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] == "done")
        agent = client.post("/sessions", json={"prompt": "agent task"}).json()
        assert [c["id"] for c in client.get("/chats").json()] == [chat["id"]]
        assert chat["id"] not in [s["id"] for s in client.get("/sessions").json()]
        assert agent["kind"] == "agent"
        assert client.get(f"/chats/{agent['id']}").status_code == 404
        renamed = client.patch(f"/chats/{chat['id']}", json={"title": "Monads"}).json()
        assert renamed["title"] == "Monads"
        assert client.delete(f"/chats/{chat['id']}").status_code == 200
        assert client.get("/chats").json() == []


def test_chat_denies_agent_tools_without_approval_cards(tmp_path):
    policy = ChatPolicy()
    assert policy.decide("web_search", {}).action == "allow"
    for name in ("run_shell", "Bash", "write_file", "git_push", "remote_control_start", "Edit"):
        assert policy.decide(name, {}).action == "deny"
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    with client:
        chat = client.post("/chats", json={"prompt": "hi"}).json()
        s = m.db.get_session(chat["id"])
        names = {t["function"]["name"] for t in Runner.tool_schemas(m.runner, s, None)}
        assert names <= {"web_search", "web_fetch"}
        assert "plain chat" in s["context"][0]["content"]
        assert "cannot run it" in s["context"][0]["content"]


def test_chat_owner_only(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="ok")])
    guest = "buddy@example.com"
    m.cfg.guests = [GuestAccess(login=guest, until=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())]
    gh = {"Tailscale-User-Login": guest}
    with client:
        chat = client.post("/chats", json={"prompt": "secret"}, headers=OWNER).json()
        assert client.get("/chats", headers=gh).status_code == 403
        assert client.post("/chats", json={"prompt": "x"}, headers=gh).status_code == 403
        assert client.get(f"/chats/{chat['id']}", headers=gh).status_code in (403, 404)
        assert client.get(f"/sessions/{chat['id']}", headers=gh).status_code == 404
        assert client.get("/sessions", headers=gh).json() == []


def test_web_shell_has_chat_home_and_drawer(tmp_path):
    client, _, _ = make_client(tmp_path, [Completion(content="ok")])
    with client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
        css = client.get("/static/style.css").text
    order = [html.index(f'data-nav="{n}"') for n in ("chat", "agents", "jobs", "images", "actions")]
    assert order == sorted(order)
    assert ">Tasks</a>" in html
    assert 'id="menu-btn"' in html
    assert 'aria-label="Open navigation menu"' in html
    assert html.index('id="drawer-profile"') > html.index('id="drawer-chats"')
    assert 'go(canChat() ? "#/chat" : "#/agents", true)' in js
    assert 'parts[0] === "chat"' in js
    assert 'event.key === "Escape"' in js
    assert "visualViewport" in js
    assert "`/chats/${encodeURIComponent(id)}/events?after=${lastSeq}`" in js
    assert "safe-area-inset-bottom" in css
    assert "#nav-drawer" in css
    assert ".chat-welcome" in css


def test_household_member_cannot_use_chat():
    from harness.access import member_forbidden
    from harness.principal import Principal
    member = Principal(kind="member", user_id="u1", allowed=True, login="kid@example.com")
    assert member_forbidden(member, "GET", "/chats") == "Chat is only available to the owner"
    assert member_forbidden(member, "POST", "/chats/abc/messages") == "Chat is only available to the owner"


def test_search_index_and_tools_exclude_chat_kind(tmp_path):
    db = Database(tmp_path / "h.sqlite3")
    seed(db, "agent00001", "scratch", "Agent task", [
        ("user_message", {"content": f"keep {AGENT_MARKER} in the agent session"}),
        ("status", {"status": "done", "answer": f"stored {AGENT_MARKER}"}),
    ], created=1)
    seed(db, "chat000001", "scratch", "Private chat", [
        ("user_message", {"content": f"keep {CHAT_MARKER} in this chat"}),
        ("status", {"status": "done", "answer": f"stored {CHAT_MARKER}"}),
    ], created=2)
    db.update_session("chat000001", kind="chat")
    db._build_search_index()
    agent_hits = search(db, CHAT_MARKER)
    assert agent_hits["results"] == []
    assert search(db, AGENT_MARKER)["results"][0]["id"] == "agent00001"
    chat_hits = search(db, CHAT_MARKER, session_kind="chat")
    assert [r["id"] for r in chat_hits["results"]] == ["chat000001"]
    assert search(db, AGENT_MARKER, session_kind="chat")["results"] == []
    assert db.session_brief("chat000001") is None
    assert db.session_brief("chat000001", kind="chat")["id"] == "chat000001"
    assert db.find_session_ids("chat000001", kind="agent") == []
    assert db.find_session_ids("agent00001", kind="agent") == ["agent00001"]
    tools = SessionSearch(db)
    assert_no_chat = tools.session_search(CHAT_MARKER, _session="agent00001")
    assert assert_no_chat.startswith("No earlier sessions match")
    assert "chat000001" not in assert_no_chat
    found_agent = tools.session_search(AGENT_MARKER, _session="chat000001")
    assert "agent00001" in found_agent
    with pytest.raises(ToolError, match="no session matches"):
        tools.session_read("chat000001", _session="agent00001")
    assert AGENT_MARKER in tools.session_read("agent00001", _session="chat000001")


def test_http_agent_surfaces_reject_chat_ids_and_search(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.allowed_logins = [LOGIN]
    cfg.search = SearchConfig(enabled=True)
    m = Manager(cfg, chat=Script([Completion(content="chat reply"), Completion(content="agent reply")]))
    m._spawn = lambda *_args, **_kwargs: None
    client = TestClient(create_app(m))
    with client:
        chat = client.post("/chats", json={"prompt": f"private {CHAT_MARKER} notes"}).json()
        agent = client.post("/sessions", json={"prompt": f"task {AGENT_MARKER} work"}).json()
        assert chat["kind"] == "chat"
        assert agent["kind"] == "agent"
        wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] in ("done", "queued", "running", "failed"))
        hits = client.get("/search", params={"q": CHAT_MARKER}).json()
        assert hits["results"] == []
        agent_hits = client.get("/search", params={"q": AGENT_MARKER}).json()
        assert [r["id"] for r in agent_hits["results"]] == [agent["id"]]
        app = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions", "sessions:all"]}).json()
        auth = {"Authorization": f"Bearer {app['key']}"}
        assert client.get("/api/v1/search", params={"q": CHAT_MARKER}, headers=auth).json()["results"] == []
        for spec in _CHAT_ON_AGENT_ROUTES:
            method, template, extra = spec[0], spec[1], spec[2] if len(spec) > 2 else None
            path = template.format(id=chat["id"])
            body = extra if method in ("POST", "PATCH", "PUT") and isinstance(extra, dict) and "follow" not in extra else None
            params = extra if isinstance(extra, dict) and "follow" in extra else None
            r = _request(client, method, path, body=body, params=params)
            assert r.status_code in (400, 404), f"{method} {path} returned {r.status_code}: {r.text}"
        for spec in _AGENT_ON_CHAT_ROUTES:
            method, template, extra = spec[0], spec[1], spec[2] if len(spec) > 2 else None
            path = template.format(id=agent["id"])
            body = extra if method in ("POST", "PATCH", "PUT", "DELETE") and isinstance(extra, dict) and "follow" not in extra else None
            params = extra if isinstance(extra, dict) and "follow" in extra else None
            r = _request(client, method, path, body=body, params=params)
            assert r.status_code == 404, f"{method} {path} returned {r.status_code}: {r.text}"
        # Agent APIs stay backward compatible, and chats keep their own event stream.
        assert client.get(f"/sessions/{agent['id']}").json()["id"] == agent["id"]
        replay = client.get(f"/chats/{chat['id']}/events", params={"follow": False})
        assert replay.status_code == 200
        assert CHAT_MARKER in replay.text
        assert chat["id"] not in [s["id"] for s in client.get("/queue").json()]


def _remote_view(logged_in=True, available=True, model="opus", popular=("sonnet",)):
    return {"available": available, "logged_in": logged_in, "model": model, "effort": None,
            "notice": "n", "billing_warning": "w", "limits": None,
            "popular_models": [{"id": p} for p in popular]}


def test_chat_options_lists_ready_remote_backends(tmp_path, monkeypatch):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    views = {"claude": _remote_view(), "codex": _remote_view(logged_in=False), "cursor": _remote_view(available=False)}
    monkeypatch.setattr("harness.backend_state.view", lambda _m, name: views[name])
    m.cfg.backends = {name: None for name in (*views, "aider")}  # aider is not a chat backend and is skipped
    m.cfg.modules.local_model = False
    with client:
        opts = client.get("/chats/options").json()
    assert opts["default_backend"] == "claude"
    assert [b["name"] for b in opts["backends"]] == ["claude"]
    claude = opts["backends"][0]
    assert claude["models"] == ["opus", "sonnet"]
    assert claude["efforts"] == ["low", "medium", "high"]
    assert claude["effort"] == ""
    assert claude["limits"] == {}


def test_chat_options_default_falls_back_when_local_unavailable(tmp_path, monkeypatch):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    monkeypatch.setattr("harness.backend_state.view", lambda _m, _name: _remote_view(popular=("opus", "sonnet")))
    m.cfg.backends = {"codex": None}
    m.cfg.models = {}  # local model configured but nothing to run: it is not offered, so the default moves on
    assert m.cfg.modules.local_model
    opts = m.chat_options()
    assert opts["default_backend"] == "codex"
    assert opts["backends"][0]["models"] == ["opus", "sonnet"]
    m.cfg.backends = {}
    assert m.chat_options() == {"default_backend": "", "backends": []}


def test_chat_delete_rename_and_cancel_guards(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    m._spawn = lambda *_a, **_k: None  # keep sessions queued, with no live task
    with client:
        chat = client.post("/chats", json={"prompt": "hello"}).json()
        agent = client.post("/sessions", json={"prompt": "task"}).json()
        assert chat["status"] == "queued"
        assert client.delete(f"/chats/{chat['id']}").status_code == 409
        assert client.patch(f"/chats/{chat['id']}", json={"title": "   "}).status_code == 400
        assert client.patch(f"/chats/{chat['id']}", json={"title": "x" * 121}).status_code == 400
        with pytest.raises(HarnessError) as err:
            m.delete_chat(agent["id"])
        assert err.value.status == 404
        assert client.post(f"/chats/{chat['id']}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/chats/{chat['id']}/cancel").status_code == 409
        assert client.post(f"/sessions/{agent['id']}/cancel").json()["status"] == "cancelled"
        assert client.delete(f"/chats/{chat['id']}").status_code == 200


def test_chat_create_is_owner_only_and_uses_chat_toolkit(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    m._spawn = lambda *_a, **_k: None
    with client:
        with pytest.raises(HarnessError) as err:
            m.create("hi", owner_id="guest:someone@example.com", kind="chat")
        assert err.value.status == 403
        chat = client.post("/chats", json={"prompt": "hi"}).json()
        s = m.db.get_session(chat["id"])
        assert isinstance(m.runner.policy(s), ChatPolicy)
        assert ChatPolicy().fingerprint() == "chat-allowlist"
        assert m.runner.daemon_toolkits(s) == ([m.runner.web] if m.runner.web is not None else [])
        m.runner.web, web = None, m.runner.web
        assert m.runner.daemon_toolkits(s) == []
        m.runner.web = web


def test_queue_lists_only_owned_agent_sessions(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    m._spawn = lambda *_a, **_k: None
    with client:
        chat = client.post("/chats", json={"prompt": "hi"}).json()
        agent = client.post("/sessions", json={"prompt": "task"}).json()
        key = client.post("/keys", json={"name": "shop", "kind": "app", "scopes": ["sessions", "sessions:all"]}).json()
        m.scheduler.positions = lambda: {agent["id"]: 0, chat["id"]: 1}
        assert client.get("/queue").json() == [{"session_id": agent["id"], "position": 0}]
        app_queue = client.get("/api/v1/queue", headers={"Authorization": f"Bearer {key['key']}"})
        assert app_queue.json() == [{"session_id": agent["id"], "position": 0}]


@pytest.mark.parametrize("path", ["/events", "/api/v1/events"])
def test_session_list_stream_hides_chats_and_run_payloads(tmp_path, path):
    client, m, _ = make_client(tmp_path, [Completion(content="")])
    m._spawn = lambda *_a, **_k: None
    with client:
        chat = client.post("/chats", json={"prompt": "hi"}).json()
        agent = client.post("/sessions", json={"prompt": "task"}).json()
        endpoint = next(r.endpoint for r in client.app.routes if getattr(r, "path", "") == path)
        owner = Principal(kind="owner", user_id="owner", allowed=True, login=LOGIN)

        async def first_event():
            async def connected():
                return False
            request = SimpleNamespace(app=client.app, state=SimpleNamespace(access=owner), headers={},
                                      is_disconnected=connected)
            body = (await endpoint(request)).body_iterator
            assert await body.__anext__() == ": connected\n\n"
            m.bus.emit(chat["id"], "status", {"status": "done"})
            m.bus.emit(agent["id"], "run_finished", {"run": {"secret": 1}, "ok": True})
            chunk = await asyncio.wait_for(body.__anext__(), 5)
            await body.aclose()
            return chunk

        chunk = asyncio.run(first_event())
    assert "run_finished" in chunk
    assert '"ok": true' in chunk
    assert chat["id"] not in chunk
    assert ("secret" in chunk) == (path == "/api/v1/events")  # only the bundled list stream strips run payloads


def test_chat_accepts_follow_up_messages(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="first"), Completion(content="second")])
    with client:
        chat = client.post("/chats", json={"prompt": "hello"}).json()
        wait_for(lambda: client.get(f"/chats/{chat['id']}").json()["status"] == "done")
        sent = client.post(f"/chats/{chat['id']}/messages", json={"content": "and again"})
        assert sent.status_code == 200
        wait_for(lambda: m.db.get_session(chat["id"])["status"] == "done"
                 and "second" in str(m.db.get_session(chat["id"]).get("answer") or ""))
