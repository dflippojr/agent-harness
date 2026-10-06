"""Issue #334 stage (c): session search is an add-on module. Present it behaves as before; absent the daemon runs
without /search, the session tools and the setting, while the index in the session database keeps filling."""

from __future__ import annotations

from fastapi.testclient import TestClient

from harness import cli
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness.settings_keys import build_registry

from test_daemon import Script, make_cfg
from test_modules import route_paths


def make(tmp_path, packages=None) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.search.enabled = True
    cfg.module_packages = packages
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def test_search_registers_through_the_interface(tmp_path):
    m = make(tmp_path)
    runtime = m.modules.get("search")
    assert runtime is not None and runtime.service is m.search
    paths = route_paths(create_app(m))
    assert {"/search", "/api/v1/search"} <= paths
    assert "search.enabled" in build_registry(m.cfg).specs
    assert any(row[0] == "search" for row in cli.admin_commands())
    gate, kit = m.modules.toolkits()[0]
    assert gate.members and gate.mcp and kit.tool_names == ("session_search", "session_read")


def test_search_switched_off_has_no_toolkit_but_keeps_its_setting(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.search.enabled = False
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    assert m.search is None and m.modules.toolkits() == []
    assert "search.enabled" in build_registry(m.cfg).specs
    with TestClient(create_app(m)) as client:
        assert client.get("/search", params={"q": "x"}).status_code == 400


def test_absent_search_runs_without_it_and_keeps_indexing(tmp_path):
    m = make(tmp_path, packages=["harness_modules.images"])
    assert "search" not in m.modules and m.search is None
    paths = route_paths(create_app(m))
    assert "/search" not in paths and "/api/v1/search" not in paths
    assert "search.enabled" not in build_registry(m.cfg).specs
    assert m.modules.toolkits() == []
    from harness.search_index import event_text
    assert event_text("user_message", {"content": "pineapple"}) == ("message", "pineapple")
    assert event_text("tool_result", {"name": "session_search", "output": "x"}) is None
