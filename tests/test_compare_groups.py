"""Issue #166: compare groups start one session per backend choice and pick or discard as a unit."""

from __future__ import annotations

import asyncio

import pytest

from harness.config import BackendConfig, Project
from harness.manager import HarnessError, Manager
from harness.llm import Completion

from test_daemon import Script, make_cfg


def make_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.projects["repo"] = Project(name="repo", repo="https://example.invalid/r.git")
    for name in ("claude", "codex"):
        cfg.backends[name] = BackendConfig(enabled=True, model="m-" + name)
    m = Manager(cfg, chat=Script([Completion(content="x")]))
    m._spawn = lambda sid: None
    return m


CHOICES = [{"backend": "claude"}, {"backend": "codex"}]


def test_group_starts_one_session_per_choice(tmp_path):
    m = make_manager(tmp_path)
    view = m.create_compare("do it", CHOICES, "repo")
    assert [r["backend"] for r in view["members"]] == ["claude", "codex"]
    ids = [r["id"] for r in view["members"]]
    assert {m.db.get_session(i)["compare_group"] for i in ids} == {view["group"]}
    assert len({m.db.get_session(i)["workspace"] for i in ids}) == 2


def test_group_size_project_and_owner_rules(tmp_path):
    m = make_manager(tmp_path)
    for bad in ([], CHOICES[:1], CHOICES * 3):
        with pytest.raises(HarnessError) as e:
            m.create_compare("p", bad, "repo")
        assert e.value.status == 400
    with pytest.raises(HarnessError):
        m.create_compare("p", CHOICES, "scratch")
    with pytest.raises(HarnessError) as e:
        m.create_compare("p", CHOICES, "repo", owner_id="someone")
    assert e.value.status == 403


def test_reported_limit_refuses_but_unknown_usage_allows(tmp_path):
    m = make_manager(tmp_path)
    m.db.set_backend_usage("codex", {"status": "rejected"})
    with pytest.raises(HarnessError) as e:
        m.create_compare("p", CHOICES, "repo")
    assert e.value.status == 429
    assert m.db.list_sessions() == []
    m.db.set_backend_usage("codex", {})
    m.create_compare("p", CHOICES, "repo")


def test_pick_reviews_winner_and_discards_rest(tmp_path):
    m = make_manager(tmp_path)
    view = m.create_compare("p", CHOICES, "repo")
    calls = []

    async def fake_review(sid, action):
        calls.append((sid, action))
        m.db.update_session(sid, review={"merge": "merged", "push": "pushed", "discard": "discarded"}[action])

    m.review = fake_review
    a, b = (r["id"] for r in view["members"])
    out = asyncio.run(m.compare_pick(view["group"], a, "merge", True))
    assert calls == [(a, "merge"), (b, "discard")]
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]
    with pytest.raises(HarnessError):
        asyncio.run(m.compare_pick(view["group"], "nope", "merge", False))
