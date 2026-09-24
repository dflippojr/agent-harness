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


def run(coro):
    return asyncio.run(coro)


CHOICES = [{"backend": "claude"}, {"backend": "codex"}]


def test_group_starts_one_session_per_choice(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("do it", CHOICES, "repo"))
    assert [r["backend"] for r in view["members"]] == ["claude", "codex"]
    ids = [r["id"] for r in view["members"]]
    assert {m.db.get_session(i)["compare_group"] for i in ids} == {view["group"]}
    assert len({m.db.get_session(i)["workspace"] for i in ids}) == 2


def test_group_size_project_and_owner_rules(tmp_path):
    m = make_manager(tmp_path)
    for bad in ([], CHOICES[:1], CHOICES * 3):
        with pytest.raises(HarnessError) as e:
            run(m.create_compare("p", bad, "repo"))
        assert e.value.status == 400
    with pytest.raises(HarnessError):
        run(m.create_compare("p", CHOICES, "scratch"))
    with pytest.raises(HarnessError) as e:
        run(m.create_compare("p", CHOICES, "repo", owner_id="someone"))
    assert e.value.status == 403


def test_reported_limit_refuses_but_unknown_usage_allows(tmp_path):
    m = make_manager(tmp_path)
    m.db.set_backend_usage("codex", {"status": "rejected"})
    with pytest.raises(HarnessError) as e:
        run(m.create_compare("p", CHOICES, "repo"))
    assert e.value.status == 429
    assert m.db.list_sessions() == []
    m.db.set_backend_usage("codex", {})
    run(m.create_compare("p", CHOICES, "repo"))


def test_pick_reviews_winner_and_discards_rest(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
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


def test_failed_later_member_discards_the_earlier_ones(tmp_path):
    m = make_manager(tmp_path)
    m.cfg.backends["codex"].enabled = False
    discarded = []

    async def fake_review(sid, action):
        discarded.append((sid, action))
        m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    s = m.db.get_session(s["id"])
    assert discarded == [(s["id"], "discard")]
    assert s["status"] == "cancelled" and s["compare_group"] == ""


def test_rollback_covers_non_harness_errors_and_failed_discard(tmp_path):
    m = make_manager(tmp_path)
    real, calls = m.create, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    async def bad_review(sid, action):
        raise HarnessError(409, "never checked out")

    m.create, m.review = flaky, bad_review
    with pytest.raises(RuntimeError):
        run(m.create_compare("p", CHOICES, "repo"))
    (s,) = m.db.list_sessions()
    s = m.db.get_session(s["id"])
    assert s["review"] == "discarded" and s["workspace_removed"] and s["compare_group"] == ""


def test_pick_retry_skips_merged_winner_and_reports_discard_failures(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    calls, fail = [], {"on": True}

    async def fake_review(sid, action):
        calls.append((sid, action))
        if action == "discard" and fail["on"]:
            raise HarnessError(500, "disk")
        m.db.update_session(sid, review={"merge": "merged", "discard": "discarded"}[action])

    m.review = fake_review
    with pytest.raises(HarnessError) as e:
        run(m.compare_pick(view["group"], a, "merge", True))
    assert b in str(e.value) and m.db.get_session(a)["review"] == "merged"
    fail["on"] = False
    out = run(m.compare_pick(view["group"], a, "merge", True))
    assert calls == [(a, "merge"), (b, "discard"), (b, "discard")]
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]


@pytest.mark.parametrize("action", ["merge", "push"])
def test_pick_that_does_not_complete_discards_nobody(tmp_path, action):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    calls = []

    async def fake_review(sid, act):
        calls.append((sid, act))
        # a conflicted merge returns normally with review="" ; a failed push raises
        if act == "push":
            raise HarnessError(502, "push rejected")
        m.db.update_session(sid, review="", review_detail="merge conflict in a.py")

    m.review = fake_review
    with pytest.raises(HarnessError) as e:
        run(m.compare_pick(view["group"], a, action, True))
    assert calls == [(a, action)]
    if action == "merge":
        assert "did not complete" in str(e.value) and "conflict" in str(e.value)
    assert m.db.get_session(b)["review"] != "discarded"


def test_pick_after_conflict_can_succeed_on_retry(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    state = {"ok": False}

    async def fake_review(sid, act):
        if act == "merge":
            m.db.update_session(sid, review="merged" if state["ok"] else "")
        else:
            m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.compare_pick(view["group"], a, "merge", True))
    state["ok"] = True
    out = run(m.compare_pick(view["group"], a, "merge", True))
    assert [r["review"] for r in out["members"]] == ["merged", "discarded"]


def test_group_discard_continues_past_a_failure(tmp_path):
    m = make_manager(tmp_path)
    view = run(m.create_compare("p", CHOICES, "repo"))
    a, b = (r["id"] for r in view["members"])
    seen = []

    async def fake_review(sid, action):
        seen.append(sid)
        if sid == a:
            raise HarnessError(500, "disk")
        m.db.update_session(sid, review="discarded")

    m.review = fake_review
    with pytest.raises(HarnessError):
        run(m.compare_discard(view["group"]))
    assert seen == [a, b]
