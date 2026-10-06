"""Issue #334 stage (c): notifications is an add-on module. Present, it behaves as before; absent, the daemon runs
and every notification is dropped."""

from __future__ import annotations

from fastapi.testclient import TestClient

from harness import cli, modules
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness.settings_keys import build_registry

from test_daemon import Script, make_cfg
from test_modules import route_paths


def make(tmp_path, packages=None) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def test_notifications_registers_through_the_interface(tmp_path):
    m = make(tmp_path)
    assert "notifications" in m.modules
    assert m.notifier is m.modules.get("notifications").service
    assert "/notify/test" in route_paths(create_app(m))
    registry = build_registry(m.cfg)
    for key in ("notifications.enabled", "notify.server", "notify.topic", "notify.token_file",
                "modules.notifications"):
        assert key in registry.specs, key
    assert any(row[0] == "notify test" for row in cli.admin_commands())
    assert m.cfg.capabilities()["modules"]["notifications"] == m.cfg.notify.enabled


def test_absent_notifications_runs_and_drops_everything(tmp_path):
    m = make(tmp_path, packages=["harness_modules.images"])
    assert "notifications" not in m.modules
    assert not m.notifier.enabled
    m.notifier.send({"title": "x"})  # dropped, no error
    assert m.notifier.link("/") == ""
    app = create_app(m)
    assert "/notify/test" not in route_paths(app)
    assert "notifications.enabled" not in build_registry(m.cfg).specs
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert modules.claims(m.cfg, "notifications") is None


def enabled_manager(tmp_path, token_file="") -> Manager:
    m = make(tmp_path)
    m.cfg.notify.enabled = True
    m.cfg.notify.server = "http://ntfy.test/"
    m.cfg.notify.topic = "t"
    m.cfg.notify.token_file = token_file
    return m


def test_notify_test_route_publishes_or_refuses(tmp_path):
    m = enabled_manager(tmp_path)
    sent = []

    async def fake_publish(client, payload):
        sent.append(payload)
    m.notifier.publish = fake_publish
    with TestClient(create_app(m)) as client:
        assert client.post("/notify/test").json() == {"sent": True}
        assert sent[0]["topic"] == "t" and sent[0]["title"] == "Agent harness"
        m.cfg.notify.enabled = False
        assert client.post("/notify/test").status_code == 400


def test_settings_checks_and_accessors(tmp_path):
    from harness_modules.notifications import settings as ns
    m = enabled_manager(tmp_path)
    assert ns.check_notifications(m.cfg) == []
    m.cfg.notify.server, m.cfg.notify.topic, m.cfg.notify.token_file = "ntfy", " ", str(tmp_path / "missing")
    assert len(ns.check_notifications(m.cfg)) == 3
    specs = {s.key: s for s in ns.specs()}
    enabled = specs["notifications.enabled"]
    enabled.setter(m.cfg, False)
    assert enabled.getter(m.cfg) is False
    hidden = specs["notify.topic"]
    assert hidden.getter(m.cfg) is None
    try:
        hidden.setter(m.cfg, "x")
    except ValueError:
        pass
    else:
        raise AssertionError("hidden notify keys are not writable")
    assert modules.is_present(m.cfg, modules.claims(m.cfg, "notifications"))


def test_service_queues_publishes_and_retries(tmp_path):
    import asyncio
    import httpx
    m = enabled_manager(tmp_path, token_file=str(tmp_path / "token"))
    (tmp_path / "token").write_text(" secret \n", encoding="utf-8")
    n = m.notifier
    assert n._token() == "secret"
    m.cfg.notify.token_file = str(tmp_path / "nope")
    assert n._token() == ""
    m.cfg.notify.token_file = str(tmp_path / "token")
    n.send({"title": "queued"})
    assert n.queue.get_nowait() == {"payload": {"title": "queued"}}
    n.listener({"type": "ignored_type", "session_id": ""})
    assert n.queue.empty()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(500 if len(seen) == 1 else 200)

    async def go():
        async def no_sleep(_):
            return None
        real = asyncio.sleep
        asyncio.sleep = no_sleep
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await n.publish(client, {"title": "p"})
        finally:
            asyncio.sleep = real
        n.send({"payload-less": 1})  # a bare dict payload goes through build() == None in _run
        n.queue.get_nowait()
        n.start()
        n.send({"title": "via run"})
        for _ in range(100):
            if n.queue.empty():
                break
            await asyncio.sleep(0.01)
        await n.stop()
        await n.stop()
    asyncio.run(go())
    assert seen[:2] == ["Bearer secret", "Bearer secret"]
    assert n.sent[0] == {"title": "p"}


def test_listener_drops_chat_and_member_sessions(tmp_path):
    m = enabled_manager(tmp_path)
    n = m.notifier
    n.db = type("Db", (), {"get_session": staticmethod(lambda sid: {
        "kind": {"c": "chat", "m": "agent", "a": "agent"}[sid], "owner_id": "u1" if sid == "m" else "owner",
        "app_id": "x" if sid == "a" else ""})})()
    for sid in "cma":
        n.listener({"type": "run_finished", "session_id": sid, "data": {}})
        assert n.queue.empty(), sid
