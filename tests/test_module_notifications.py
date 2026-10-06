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
