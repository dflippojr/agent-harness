"""Skills package presence, settings, tool isolation and review lifecycle."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.api import create_app
from harness.manager import Manager
from harness.settings_keys import build_registry
from harness_modules.skills.settings import check_skills
from test_daemon import make_cfg
from test_modules import route_paths


def make(tmp_path, enabled=True, packages=None):
    cfg = make_cfg(tmp_path)
    cfg.skills.enabled = enabled
    cfg.skills.local_review = False
    cfg.module_packages = packages
    cfg.memory_library.enabled = False
    cfg.remote_control.enabled = False
    cfg.runners = {}
    cfg.notify.enabled = False
    cfg.gpu_guard.enabled = False
    return Manager(cfg)


def test_registration_and_eligibility(tmp_path):
    m = make(tmp_path)
    rt = m.modules.get("skills")
    assert rt.service is m.skills
    gate, kit = next((g, k) for g, k in m.modules.toolkits() if k is m.skills)
    session = {"id": "s", "owner_id": "owner", "app_id": "", "job_id": ""}
    assert gate.eligible(kit, session) and not gate.mcp
    for key, value in (("app_id", "app"), ("job_id", "job"), ("owner_id", "member"), ("kind", "chat")):
        assert not gate.eligible(kit, {**session, key: value})
    paths = route_paths(create_app(m))
    assert {"/skills", "/skills/{slug}/projects"} <= paths
    with TestClient(create_app(m)) as client:
        assert client.get("/api/admin/v1/skills").status_code == 200
    assert any(row[0] == "skills install" for row in cli.admin_commands())
    spec = build_registry(m.cfg).get("skills.enabled")
    assert spec.getter(m.cfg) is True
    spec.setter(m.cfg, False)
    assert spec.getter(m.cfg) is False
    assert check_skills(m.cfg) == []


@pytest.mark.parametrize("packages", [None, ["harness_modules.images"]])
def test_disabled_and_absent(tmp_path, packages):
    m = make(tmp_path, enabled=False, packages=packages)
    present = packages is None
    assert m.skills is None
    assert ("skills.enabled" in build_registry(m.cfg).specs) is present
    with TestClient(create_app(m)) as client:
        assert client.get("/health").status_code == 200
        for prefix in ("", "/api/admin/v1"):
            response = client.get(prefix + "/skills")
            assert response.status_code == (200 if present else 404)
            if present:
                assert response.json() == {"enabled": False, "proposals": [], "installed": []}
                assert client.get(prefix + "/skills/enabled").json() == []
                assert client.post(prefix + "/skills/x/enable").status_code == 400


def test_absent_enabled_config_is_dormant(tmp_path):
    m = make(tmp_path, packages=["harness_modules.images"])
    assert m.skills is None and m.cfg.skills.enabled
    assert not m.cfg.capabilities()["modules"].get("skills", False)
    assert not any(k.tool_names == ("propose_skill",) for _, k in m.modules.toolkits())
    assert "harness_skills_installed" not in __import__("harness.metrics", fromlist=["render"]).render(m)


def test_review_lifecycle_and_gpu_idle(tmp_path):
    m = make(tmp_path)
    rt = m.modules.get("skills")
    assert rt.idle()
    m.scheduler.paused = True
    assert not rt.idle()
    m.scheduler.paused = False
    m.runner.generating.add("s")
    assert not rt.idle()
    m.runner.generating.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(m.modules, "busy", lambda: True)
        assert not rt.idle()
    m.guard = SimpleNamespace(active=True, manual=False)
    assert not rt.idle()
    m.guard = None
    rt.service = Mock(reviewer=Mock(stop=AsyncMock()))
    rt.start()
    rt.service.reconcile.assert_called_once()
    rt.service.reviewer.start.assert_called_once()
    asyncio.run(rt.stop())
    rt.service.reviewer.stop.assert_awaited_once()
    rt.service.reviewer = None
    rt.start()
    asyncio.run(rt.stop())


@pytest.mark.parametrize("with_key", [False, True])
def test_hosted_review_uses_config_and_temp_key(tmp_path, monkeypatch, with_key):
    m = make(tmp_path)
    m.cfg.skills.reviewer_base_url = "https://review.invalid/"
    m.cfg.skills.reviewer_model = "reviewer"
    if with_key:
        key = tmp_path / "key"
        key.write_text("test-key\n", encoding="utf-8")
        m.cfg.skills.reviewer_api_key_file = str(key)
    async def post(client, url, **kwargs):
        assert url == "https://review.invalid/v1/chat/completions"
        assert kwargs["json"]["model"] == "reviewer"
        assert kwargs["headers"] == ({"Authorization": "Bearer test-key"} if with_key else {})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(m.modules.get("skills").hosted_review([])).content == "ok"


def test_owner_proposal_lifecycle_on_admin_api(tmp_path):
    from test_skills import EXAMPLES, SKILL_MD
    from harness_modules.skills.service import in_process_sandbox
    m = make(tmp_path)
    store = m.skills
    store._run_sandbox = in_process_sandbox
    session = {"id": "s", "owner_id": "owner", "app_id": "", "job_id": ""}
    import json
    asyncio.run(store.propose_from_tool({"slug": "commit-style", "title": "Commit style",
        "purpose": "Keep git commit messages conventional and short.",
        "skill_md": SKILL_MD, "examples": json.dumps(EXAMPLES)}, session))
    row = store.db.list_skill_proposals()[0]
    base = "/api/admin/v1/skills"
    proposal = base + "/proposals/" + row["id"]
    with TestClient(create_app(m)) as client:
        assert client.get(proposal).status_code == 200
        assert client.post(proposal + "/review").status_code == 400
        assert client.post(proposal + "/reject", json={"reason": "revise"}).status_code == 200
        assert client.post(proposal + "/reopen").status_code == 200
        assert client.post(proposal + "/install", json={"content_hash": row["content_hash"]}).status_code == 200
        slug = base + "/commit-style"
        assert client.post(slug + "/enable").status_code == 200
        assert client.put(slug + "/projects", json={"projects": ["scratch"]}).status_code == 200
        assert client.get(base + "/enabled").json()[0]["slug"] == "commit-style"
        assert client.get(slug + "/export").status_code == 200
        assert client.post(slug + "/rollback").status_code == 409
        assert client.post(slug + "/disable").status_code == 200
        assert client.post(slug + "/uninstall").status_code == 200
        assert client.delete(proposal).status_code == 409
        asyncio.run(store.propose_from_tool({"slug": "another-style", "title": "Another style",
            "purpose": "Keep messages concise.", "skill_md": SKILL_MD,
            "examples": json.dumps(EXAMPLES)}, session))
        draft = next(p for p in store.db.list_skill_proposals() if p["slug"] == "another-style")
        draft_url = base + "/proposals/" + draft["id"]
        assert client.delete(draft_url).status_code == 204
        assert client.get(draft_url).status_code == 404
