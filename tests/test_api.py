"""Phase 2 API tests: web app, security guard, notifications, token approvals, templates, changes, rerun."""

from __future__ import annotations

import subprocess
import time

from fastapi.testclient import TestClient

from harness.api import create_app
from harness import config as config_mod
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
        assert "<title>Agent Harness Web</title>" in client.get("/").text
        js = client.get("/static/app.js").text
        assert 'go(name === "transcript" ? `#/s/${sid}` : `#/s/${sid}/${name}`, true)' in js
        assert "session-chrome" in js and "jump-top" in js and 'method: "PATCH"' in js
        assert "session-title" in js
        assert "harness.theme" in js
        assert "harness.textSize" in js and "TEXT_SIZES" in js and "applyTextSize" in js
        assert 'setHeader("agents", "New task", { page: true })' in js
        assert "0.75 * window.innerHeight" in js
        assert 'type: "color"' not in js
        assert "swatch split" in js
        assert "Scratch is a fresh empty folder" in js
        assert '"＋ New project"' in js and 'api("/projects", { method: "POST"' in js
        assert "Only tower projects with a local folder appear" in js
        assert 'id="guest-banner"' in client.get("/").text
        assert 'id="bar"' in client.get("/").text
        assert 'id="feature-nav"' in client.get("/").text
        assert "harness.textSize" in client.get("/").text
        assert "paintGuestChrome" in js and "isGuest()" in js
        css = client.get("/static/style.css").text
        assert "safe-area-inset-top, 0px) + 18px" in css
        assert "-webkit-transform: translate3d(0, 0, 0)" in css
        assert "#bar.paint-refresh" in css and "#bar > *" in css
        assert 'window.addEventListener("pageshow", repaintBar)' in js
        assert 'window.addEventListener("orientationchange", repaintBar)' in js
        assert "if (!document.hidden) repaintBar()" in js
        assert ".session-chrome" in css and ".jump-top" in css
        assert ".swatch.split" in css and ".hue-preview" in css
        assert "#fab-host" in css and "width: 9.75rem" in css
        assert "#guest-banner" in css
        assert "--text-scale" in css and "max(16px, 1rem)" in css
        assert ".size-grid" in css
        assert ".action-item" in css and ".action-subitem.disabled" in css and ".switch:checked" in css
        assert "prefers-reduced-motion: reduce" in css
        assert ".image-status .progress.indeterminate > span" in css
        assert 'showFab("#/new", "+ New task")' in js
        assert 'showFab("#/jobs/new", "+ New job")' in js
        assert 'api("/backends?auth=skip")' in js
        assert 'if (images && !route.onImages) api("/images/warmup"' not in js
        assert "function imageModeEntries" in js
        assert 'disabled: spec.available === false' in js
        assert "quality-fast" in js
        assert 'if (prompt.value.trim()) startWarmup()' in js
        assert 'await startWarmup().catch(() => {})' in js
        assert "updateImageStatusView(phase, d.status)" in js
        assert "data.status.modes" in js
        assert "not installed" in js
        assert "updateFluxHint" in js
        assert "That image mode isn't installed" in js
        assert "grid.dataset.keys" in js
        assert 'upscaling ? "Upscaling"' in js and 'editing ? "Editing"' in js
        assert "function maskEditor" in js and "viewImageEdit" in js
        assert "canvas.width / r.width" in js
        assert "Independent backups are not changed" in js
        assert ".mask-stage" in css and "touch-action: none" in css
        assert '"Upscale 2×"' in js and '"Upscale 4×"' in js
        assert "upscale: upscale.value" in js
        assert 'href: "#/profile/account"' in js
        assert "gpuActionRow()" in js and "function gpuCard()" not in js
        assert 'h("span", {}, "Duration:")' in js and 'duration.disabled = isGuest() || !g.manual' in js
        assert 'href: "#/profile/remote-control"' in js and 'href: "#/profile/disk"' in js
        assert '"smart-approvals": "Smart approvals"' in js and "function smartApprovalsCard()" in js
        assert 'skills: "Skills"' in js
        assert 'api("/skills/enabled")' in js
        assert "Install hash" in js
        assert "confirmGpuQueue" in js and "gpu.manual" in js
        assert "picker.hidden = !picker.hidden" not in js
        assert "if (holding)" in js
        assert 'id="fab-host"' in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        profile = client.get("/profile").json()
        assert profile["emoji"] == "🙂" and "🚀" in profile["choices"]
        assert client.put("/profile", json={"emoji": "🚀"}).json()["emoji"] == "🚀"
        assert client.get("/profile").json()["emoji"] == "🚀"
        assert client.put("/profile", json={"emoji": "nope"}).status_code == 400
        assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")
        manifest = client.get("/manifest.webmanifest").json()
        assert manifest["display"] == "standalone"
        assert manifest["name"] == "Agent Harness Web"
        assert manifest["short_name"] == "Harness"
        # tailnet identity
        assert client.get("/sessions", headers={"Tailscale-User-Login": "intruder@example.com"}).status_code == 403
        me = client.get("/me", headers={"Tailscale-User-Login": LOGIN}).json()
        assert me["login"] == LOGIN and me["role"] == "owner" and me["guest_until"] is None
        # cross-site browser POSTs are refused; same-origin and non-browser clients are fine
        body = {"prompt": "hello"}
        assert client.post("/sessions", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/sessions", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/sessions", json=body, headers={"Origin": PUBLIC}).status_code == 201
        assert client.post("/sessions", json=body).status_code == 201


def test_owner_created_project_overlay_roundtrip(tmp_path):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    (config_dir / "harness.yaml").write_text(
        "default_model: fake\nmodels:\n  fake:\n    base_url: http://127.0.0.1:1\n", encoding="utf-8")
    public_projects = "projects:\n  scratch:\n    description: Built in\n"
    (config_dir / "projects.yaml").write_text(public_projects, encoding="utf-8")

    cfg = config_mod.load(config_dir, data_dir)
    saved = config_mod.add_project(cfg, config_mod.Project(
        name="My-Repo", description="  Reviewable work  ", target="tower",
        repo="https://example.test/owner/repo.git", owner_id="owner"))
    assert saved.name == "my-repo" and saved.managed
    assert cfg.projects["my-repo"].repo.endswith("repo.git")
    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == public_projects
    assert "my-repo" in (data_dir / "projects.yaml").read_text(encoding="utf-8")

    reloaded = config_mod.load(config_dir, data_dir)
    assert reloaded.projects["scratch"].managed is False
    assert reloaded.projects["my-repo"].managed is True
    assert reloaded.projects["my-repo"].owner_id == "owner"


def test_create_project_api_is_hot_and_private(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="hi")])
    owner = {"Tailscale-User-Login": LOGIN}
    with client:
        created = client.post("/projects", headers=owner, json={
            "name": "notes", "description": "Personal notes", "target": "tower", "repo": "",
        })
        assert created.status_code == 201
        assert created.json() == {"name": "notes", "description": "Personal notes", "repo": False,
                                  "homelab": False, "target": "tower", "managed": True}
        assert any(p["name"] == "notes" for p in client.get("/projects", headers=owner).json())
        assert (m.cfg.data_dir / "projects.yaml").exists()
        session = client.post("/sessions", headers=owner, json={"prompt": "hello", "project": "notes"})
        assert session.status_code == 201
        assert m.db.get_session(session.json()["id"])["owner_id"] == "owner"
        assert client.post("/projects", headers=owner, json={"name": "notes"}).status_code == 400
        assert client.post("/projects", headers=owner, json={"name": "Not a slug"}).status_code == 400
        assert client.post("/projects", headers=owner,
                           json={"name": "away", "target": "macbook"}).status_code == 400


def test_rename_session(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="hi")])
    with client:
        sid = client.post("/sessions", json={"prompt": "Make dark mode black"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        renamed = client.patch(f"/sessions/{sid}", json={"title": "  Dark mode  "}).json()
        assert renamed["title"] == "Dark mode"
        assert client.get(f"/sessions/{sid}").json()["title"] == "Dark mode"
        assert client.get("/sessions").json()[0]["title"] == "Dark mode"
        assert client.put(f"/sessions/{sid}", json={"title": "Palette"}).json()["title"] == "Palette"
        assert client.patch(f"/sessions/{sid}", json={"title": "   "}).status_code == 400
        assert client.patch(f"/sessions/{sid}", json={"title": "x" * 121}).status_code == 400


def test_session_list_has_whole_chat_summary(tmp_path):
    client, m, _ = make_client(tmp_path, [Completion(content="Changed the palette."),
                                          Completion(content="Moved the control to the header.")])
    with client:
        sid = client.post("/sessions", json={"prompt": "Make dark mode black"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        client.post(f"/sessions/{sid}/messages", json={"content": "Also move the profile control"})
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        card = next(item for item in client.get("/sessions").json() if item["id"] == sid)
        assert "Make dark mode black" in card["chat_summary"]
        assert "Also move the profile control" in card["chat_summary"]
        assert "Moved the control to the header" in card["chat_summary"]
        assert "answer_preview" not in card


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


def test_sleeping_model_is_announced_and_warmed(tmp_path):
    from harness.warmup import READY, SLEEPING

    client, m, sent = make_client(tmp_path, [Completion(content="hello")])
    states = {"now": SLEEPING}
    warmed = []

    async def fake_state(model):
        return states["now"]

    async def fake_wake(model):
        warmed.append(model.name)
        states["now"] = READY

    m.warmer.state = fake_state
    m.warmer._wake = fake_wake
    with client:
        assert client.get("/models/status").json()[0]["state"] == SLEEPING
        assert client.post("/models/warm").json()["state"] == SLEEPING
        wait_for(lambda: warmed == ["fake"])
        assert client.get("/models/status").json()[0]["state"] == READY

        states["now"] = SLEEPING  # asleep again when the task starts
        sid = client.post("/sessions", json={"prompt": "hi"}).json()["id"]
        wait_for(lambda: m.db.get_session(sid)["status"] == "done")
        types = [e["type"] for e in m.db.events(sid)]
        assert types.index("model_waking") < types.index("model_ready") < types.index("assistant")
        note = wait_for(lambda: next((p for p in sent if p["title"].startswith("Waking the model")), None))
        assert note["priority"] == 2 and note["click"].endswith(f"/#/s/{sid}")
        assert "model ready after" in client.get(f"/sessions/{sid}/transcript").text


def test_guest_demo_access(tmp_path):
    from datetime import datetime, timedelta, timezone

    from harness.access import guest_forbidden, parse_guest_until, resolve_access
    from harness.config import GuestAccess

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=2)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    until = parse_guest_until(future)
    assert until is not None and until > now

    client, m, _ = make_client(tmp_path, [Completion(content="hi")])
    guest = "buddy@example.com"
    m.cfg.guests = [GuestAccess(login=guest, until=future)]
    gh = {"Tailscale-User-Login": guest, "Tailscale-User-Name": "Buddy"}
    oh = {"Tailscale-User-Login": LOGIN}

    ident = resolve_access(m.cfg, guest)
    assert ident.allowed and ident.role == "guest"
    assert guest_forbidden(ident, "GET", "/sessions") is None
    assert guest_forbidden(ident, "POST", "/sessions") == "demo access is read-only"
    assert guest_forbidden(ident, "GET", "/keys") == "demo access cannot view owner credentials"
    assert guest_forbidden(ident, "GET", "/api/admin/v1") == "demo access cannot use the owner API"
    assert guest_forbidden(ident, "POST", "/runners/macbook/poll") is None
    owner = resolve_access(m.cfg, LOGIN)
    assert owner.role == "owner" and owner.allowed
    assert resolve_access(m.cfg, None).role == "owner"
    assert resolve_access(m.cfg, "intruder@example.com").allowed is False

    with client:
        me = client.get("/me", headers=gh).json()
        assert me["role"] == "guest" and me["login"] == guest
        assert me["name"] == "Buddy" and me["notify"]["topic"] == ""
        assert me["guest_until"]
        assert client.get("/projects", headers=gh).json() == []
        assert client.get("/sessions", headers=gh).json() == []
        assert client.get("/queue", headers=gh).json() == []
        assert client.get("/search?q=hello", headers=gh).json()["results"] == []
        assert client.get("/templates", headers=gh).json() == []
        assert client.get("/maintenance", headers=gh).status_code == 403
        assert client.get("/keys", headers=gh).status_code == 403
        assert client.get("/metrics", headers=gh).status_code == 403
        assert client.get("/api/admin/v1", headers=gh).status_code == 403
        assert "owner API" in client.get("/api/admin/v1/sessions", headers=gh).json()["detail"]
        denied = client.post("/sessions", json={"prompt": "hello"}, headers=gh)
        assert denied.status_code == 403 and "read-only" in denied.json()["detail"]
        assert client.put("/profile", json={"emoji": "🚀"}, headers=gh).status_code == 403
        assert client.post("/models/warm", headers=gh).status_code == 403
        created = client.post("/sessions", json={"prompt": "hello"}, headers=oh)
        assert created.status_code == 201
        sid = created.json()["id"]
        assert m.db.get_session(sid)["owner_id"] == "owner"
        assert client.patch(f"/sessions/{sid}", json={"title": "Nope"}, headers=gh).status_code == 403
        assert client.get(f"/sessions/{sid}", headers=gh).status_code == 404
        assert client.get(f"/sessions/{sid}/transcript", headers=gh).status_code == 404
        assert client.get(f"/sessions/{sid}/changes", headers=gh).status_code == 404
        assert client.get(f"/sessions/{sid}/approvals", headers=gh).status_code == 404
        assert client.get(f"/sessions/{sid}/events?follow=false", headers=gh).status_code == 404
        assert client.post(f"/sessions/{sid}/cancel", headers=gh).status_code == 403
        assert client.post("/a/not-a-token/approve", headers=gh).status_code == 403

        m.cfg.guests = [GuestAccess(login=guest, until=past)]
        expired = client.get("/sessions", headers=gh)
        assert expired.status_code == 403 and "expired" in expired.json()["detail"]

        m.cfg.guests = [GuestAccess(login=guest, until="not-a-date")]
        assert client.get("/sessions", headers=gh).status_code == 403

        m.cfg.guests = [GuestAccess(login=LOGIN, until=future)]
        still_owner = client.get("/me", headers=oh).json()
        assert still_owner["role"] == "owner"

        m.cfg.allowed_logins = []
        m.cfg.guests = [GuestAccess(login=guest, until=future)]
        open_tailnet = client.get("/me", headers=gh).json()
        assert open_tailnet["role"] == "owner"

        from harness.config import _load_guests
        loaded = _load_guests([guest, {"login": "other@example.com", "until": future}])
        assert loaded[0].login == guest and loaded[0].until == ""
        assert loaded[1].login == "other@example.com" and loaded[1].until == future
