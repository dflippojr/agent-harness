"""Chat home: durable non-agent conversations, chat-safe tools, and owner/guest isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness.config import GuestAccess
from harness.llm import Completion
from harness.policy import ChatPolicy
from harness.runner import Runner

from test_api import LOGIN, make_client, wait_for
from test_daemon import call

OWNER = {"Tailscale-User-Login": LOGIN}


def test_chat_is_separate_from_agent_sessions(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="Hello!")])
    with client:
        opts = client.get("/chats/options").json()
        assert opts["default_backend"] == "local"
        assert opts["backends"][0]["models"] == ["fake"]
        chat = client.post("/chats", json={"prompt": "What is a monad?\nBe brief."}).json()
        assert chat["kind"] == "chat" and chat["title"] == "What is a monad?" and chat["project"] == "scratch"
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
        assert "plain chat" in s["context"][0]["content"] and "cannot run it" in s["context"][0]["content"]


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
