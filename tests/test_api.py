"""Phase 2 API tests: web app, security guard, notifications, token approvals, templates, changes, rerun."""

from __future__ import annotations

import subprocess
import time

from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import NotifyConfig
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, call, make_cfg

LOGIN = "me@example.com"
PUBLIC = "https://tower.example.ts.net"


def make_client(tmp_path, steps, rules=None):
    cfg = make_cfg(tmp_path, rules=rules)
    cfg.public_url = PUBLIC
    cfg.allowed_logins = [LOGIN]
    cfg.notify = NotifyConfig(enabled=True, server="http://127.0.0.1:9", topic="t")
    m = Manager(cfg, chat=Script(steps))
    sent: list[dict] = []

    async def fake_publish(client, payload):
        sent.append(payload)
    m.notifier.publish = fake_publish
    return TestClient(create_app(m)), m, sent


def wait_for(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_web_app_and_guard(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="hi")])
    with client:
        assert "<title>Agents</title>" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")
        assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
        # tailnet identity
        assert client.get("/sessions", headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 403
        assert client.get("/me", headers={"Tailscale-User-Login": LOGIN}).json()["login"] == LOGIN
        # cross-site browser POSTs are refused; same-origin and non-browser clients are fine
        body = {"prompt": "hello"}
        assert client.post("/sessions", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/sessions", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/sessions", json=body, headers={"Origin": PUBLIC}).status_code == 201
        assert client.post("/sessions", json=body).status_code == 201


def test_approval_notification_and_token_buttons(tmp_path):
    rules = [{"tool": "write_file", "action": "ask", "reason": "test gate"}]
    steps = [Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]), Completion(content="all done")]
    client, m, sent = make_client(tmp_path, steps, rules=rules)
    with client:
        sid = client.post("/sessions", json={"prompt": "write a file", "project": "guarded"}).json()["id"]
        note = wait_for(lambda: next((p for p in sent if p.get("actions")), None))
        approval = m.db.pending_approvals(sid)[0]
        assert note["title"].startswith("Approve?") and note["sequence_id"] == approval["id"]
        assert note["click"] == f"{PUBLIC}/#/s/{sid}/approval/{approval['id']}"
        approve_url = note["actions"][0]["url"]
        assert approve_url == f"{PUBLIC}/a/{approval['token']}/approve"
        # tokens never leak through the API
        assert "token" not in client.get(f"/sessions/{sid}").json()["pending_approvals"][0]
        assert all("token" not in a for a in client.get(f"/sessions/{sid}/approvals?all=1").json())
        # the button: POST with no body, as the ntfy app sends it
        assert client.post("/a/not-a-token/approve").status_code == 404
        path = approve_url[len(PUBLIC):]
        assert client.post(path).json()["status"] == "approved"
        assert client.post(path).json()["status"] == "approved"  # pressing twice is harmless
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        replaced = wait_for(lambda: next((p for p in sent if p.get("sequence_id") == approval["id"]
                                          and "Approved" in p["title"]), None))
        assert "a.txt" in replaced["message"]
        done = wait_for(lambda: next((p for p in sent if p["title"].startswith("Done")), None))
        assert done["message"] == "all done" and done["click"].endswith(f"/#/s/{sid}")


def test_templates_rerun_and_changes(tmp_path):
    steps = [
        Completion(tool_calls=[call("write_file", 0, path="repo/new.txt", content="hello\n")]),
        Completion(content="wrote it"),
    ]
    client, m, _ = make_client(tmp_path, steps)
    with client:
        t = client.post("/templates", json={"name": "Fix tests", "prompt": "fix the tests"}).json()
        assert client.get("/templates").json()[0]["name"] == "Fix tests"
        assert client.put(f"/templates/{t['id']}", json={"name": "Fix", "prompt": "p"}).json()["name"] == "Fix"
        assert client.post("/templates", json={"name": "x", "prompt": "y", "project": "nope"}).status_code == 400
        assert client.delete(f"/templates/{t['id']}").status_code == 204

        s = client.post("/sessions", json={"prompt": "make a file"}).json()
        repo = m.cfg.workspaces_dir / s["id"] / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        wait_for(lambda: m.db.get_session(s["id"])["status"] == "done")
        changes = client.get(f"/sessions/{s['id']}/changes").json()["repos"][0]
        assert changes["path"] == "repo" and "+hello" in changes["diff"]
        assert {"path": "new.txt", "status": "??"} in changes["files"]

        again = client.post(f"/sessions/{s['id']}/rerun").json()
        assert again["id"] != s["id"] and again["title"] == s["title"]
        assert m.original_prompt(again["id"]) == "make a file"
