"""Issue #334 stage (c): notifications is an add-on module. Present, it behaves as before; absent, the daemon runs
and every notification is dropped."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import modules
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.notifications.service import DeliveryError

from test_daemon import Script, make_cfg


def make(tmp_path, packages=None) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.module_packages = packages
    return Manager(cfg, chat=Script([Completion(content="hi")]))


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

        async def refused(client, payload):
            raise DeliveryError("ntfy answered 401")
        m.notifier.publish = refused
        response = client.post("/notify/test")
        assert response.status_code == 502
        assert response.json()["detail"] == "ntfy answered 401"
        m.cfg.notify.enabled = False
        assert client.post("/notify/test").status_code == 400


def test_settings_checks_and_hidden_keys(tmp_path):
    from harness_modules.notifications import settings as ns
    m = enabled_manager(tmp_path)
    assert ns.check_notifications(m.cfg) == []
    m.cfg.notify.server, m.cfg.notify.topic, m.cfg.notify.token_file = "ntfy", " ", str(tmp_path / "missing")
    assert len(ns.check_notifications(m.cfg)) == 3
    hidden = {s.key: s for s in ns.specs()}["notify.topic"]
    assert hidden.getter(m.cfg) is None
    with pytest.raises(ValueError):
        hidden.setter(m.cfg, "x")
    assert modules.is_present(m.cfg, modules.claims(m.cfg, "notifications"))


def test_service_token_queue_and_lifecycle(tmp_path):
    m = enabled_manager(tmp_path, token_file=str(tmp_path / "token"))
    (tmp_path / "token").write_text(" secret \n", encoding="utf-8")
    n = m.notifier
    assert n._token() == "secret"
    m.cfg.notify.token_file = str(tmp_path / "nope")
    assert n._token() == ""
    n.send({"title": "queued"})
    assert n.queue.get_nowait() == {"payload": {"title": "queued"}}
    n.listener({"type": "ignored_type", "session_id": ""})
    assert n.queue.empty()

    async def go():
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


@pytest.mark.parametrize("answers, reason, attempts, sleeps", [
    ([200], None, 1, []),
    ([503, 200], None, 2, [2]),
    ([401], "ntfy answered 401", 1, []),
    ([503], "ntfy answered 503 after 3 attempts", 3, [2, 4]),
    ([httpx.ConnectError("refused")], "ntfy unreachable after 3 attempts", 3, [2, 4]),
])
def test_publish_reports_delivery_and_retries_only_server_and_network_errors(
        tmp_path, monkeypatch, answers, reason, attempts, sleeps):
    (tmp_path / "token").write_text("secret", encoding="utf-8")
    n = enabled_manager(tmp_path, token_file=str(tmp_path / "token")).notifier
    slept, calls = [], []

    async def no_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(request)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await n.publish(client, {"title": "p"})
    if reason is None:
        asyncio.run(go())
    else:
        with pytest.raises(DeliveryError) as refused:
            asyncio.run(go())
        assert str(refused.value) == reason
    assert len(calls) == attempts
    assert {call.headers["authorization"] for call in calls} == {"Bearer secret"}
    assert slept == sleeps


def test_background_queue_survives_a_refused_notification(tmp_path, monkeypatch, caplog):
    n = enabled_manager(tmp_path).notifier
    real_client = httpx.AsyncClient
    titles = []

    def handler(request: httpx.Request) -> httpx.Response:
        titles.append(json.loads(request.content)["title"])
        return httpx.Response(401 if len(titles) == 1 else 200)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    async def go():
        n.start()
        n.send({"title": "refused"})
        n.send({"title": "after"})
        for _ in range(200):
            if len(titles) == 2:
                break
            await asyncio.sleep(0.01)
        await n.stop()
    with caplog.at_level(logging.WARNING, logger="harness.notify"):
        asyncio.run(go())
    assert titles == ["refused", "after"]
    assert "notification not delivered: ntfy answered 401" in caplog.text


def test_listener_drops_chat_and_member_sessions(tmp_path):
    m = enabled_manager(tmp_path)
    n = m.notifier
    n.db = type("Db", (), {"get_session": staticmethod(lambda sid: {
        "kind": {"c": "chat", "m": "agent", "a": "agent", "t": "tools_only"}[sid],
        "owner_id": "u1" if sid == "m" else "owner", "app_id": "x" if sid in "at" else ""})})()
    for sid in "cmat":  # #329: an App's tools-only session never reaches the owner's phone either
        n.listener({"type": "run_finished", "session_id": sid, "data": {}})
        assert n.queue.empty(), sid
