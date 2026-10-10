"""#524: session admission. No App, member or parked session can hold the shared queue, a hosted backend slot or the
GPU for an unbounded time: per-App caps, parked sessions counted against caps, backend slots released while a session
waits on an approval, an approval deadline, and a wall-clock budget for members' and Apps' runs."""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from harness.api import create_app
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.principal import OWNER_USER_ID
from harness.storage import workspaces_dir

from test_app_stores import _key
from test_daemon import Script, call, make_cfg, wait_status
from test_household import ALICE, OWNER, H, create_member, household
from test_phase6 import wait_for
from test_phase8 import _claude_manager

LIMITS = "/api/admin/v1/apps/{}/limits"


def _status(client, sid: str, auth: dict) -> str:
    return client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"]


def _insert(m, sid: str, status: str, owner_id: str = OWNER_USER_ID, app_id: str = "") -> None:
    now = time.time()
    ws = workspaces_dir(m.cfg, owner_id, app_id) / sid
    ws.mkdir(parents=True, exist_ok=True)
    m.db.insert_session({
        "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local", "title": sid,
        "status": status, "workspace": str(ws), "created_at": now, "updated_at": now,
        "context": [{"role": "user", "content": "hi"}], "run": {}, "totals": {}, "inbox": [],
        "owner_id": owner_id, "app_id": app_id,
    })


async def _until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


# per-App caps ---------------------------------------------------------------------------------------------------------
def test_an_apps_extra_queued_session_is_refused_and_a_slot_frees_when_one_ends(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        shop_id, shop = _key(client, "shop", "sessions")
        _, other = _key(client, "other", "sessions")
        r = client.put(LIMITS.format(shop_id), json={"max_queued": 2})
        assert r.status_code == 200, r.text
        assert r.json()["effective"] == {"max_running": m.cfg.app_max_running, "max_queued": 2}
        m.scheduler.set_paused(True)  # nothing runs, so every new session stays queued

        first, second = (client.post("/api/v1/sessions", headers=shop, json={"prompt": p}).json()["id"]
                         for p in ("one", "two"))
        refused = client.post("/api/v1/sessions", headers=shop, json={"prompt": "three"})
        assert refused.status_code == 429, refused.text
        assert refused.json()["error"]["code"] == "app_queue_full"
        # The owner and another App are not held to this App's cap.
        assert client.post("/sessions", json={"prompt": "mine", "project": "scratch"}).status_code == 201
        assert client.post("/api/v1/sessions", headers=other, json={"prompt": "theirs"}).status_code == 201

        assert client.post(f"/api/v1/sessions/{first}/cancel", headers=shop).status_code == 200
        wait_for(lambda: _status(client, first, shop) == "cancelled")
        third = client.post("/api/v1/sessions", headers=shop, json={"prompt": "three"})
        assert third.status_code == 201, third.text
        assert client.post("/api/v1/sessions", headers=shop, json={"prompt": "four"}).status_code == 429

        m.scheduler.set_paused(False)
        wait_for(lambda: _status(client, second, shop) == "done")
        wait_for(lambda: _status(client, third.json()["id"], shop) == "done")
        assert client.post("/api/v1/sessions", headers=shop, json={"prompt": "five"}).status_code == 201


def test_app_caps_are_owner_only_and_reset_to_the_default(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        shop_id, shop = _key(client, "shop", "sessions")
        assert client.put(LIMITS.format(shop_id), headers=shop, json={"max_queued": 50}).status_code in (401, 403)
        assert client.put(LIMITS.format(shop_id), json={"max_running": 0}).status_code == 422
        assert client.put(LIMITS.format("k-nope"), json={"max_running": 1}).status_code == 404
        set_both = client.put(LIMITS.format(shop_id), json={"max_running": 1, "max_queued": 3}).json()
        assert set_both["configured"] == {"max_running": 1, "max_queued": 3}
        # A field left out keeps its value; null puts it back to the daemon default.
        partial = client.put(LIMITS.format(shop_id), json={"max_queued": None}).json()
        assert partial["configured"] == {"max_running": 1, "max_queued": None}
        assert partial["effective"] == {"max_running": 1, "max_queued": m.cfg.app_max_queued}
        assert client.get(LIMITS.format(shop_id)).json()["effective"]["max_running"] == 1
        audit = [row["outcome"] for row in m.db.main.list_audit(50) if row["action"] == "app.limits"]
        assert sorted(audit) == ["noop", "ok", "ok"]  # the unknown App is recorded as a no-op


def test_an_apps_running_cap_counts_its_parked_sessions(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    client = TestClient(create_app(m))
    with client:
        shop_id, _ = _key(client, "shop", "sessions")
        client.put(LIMITS.format(shop_id), json={"max_running": 1})
        _insert(m, "parked0001", "waiting_approval", app_id=shop_id)
        _insert(m, "queued0001", "queued", app_id=shop_id)
        _insert(m, "owner00001", "queued")
        assert not m._scheduler_eligible("queued0001")
        assert m._scheduler_eligible("parked0001")  # its own slot: it can come back to run
        assert m._scheduler_eligible("owner00001")
        m.db.update_session("parked0001", status="done")
        assert m._scheduler_eligible("queued0001")


# members ------------------------------------------------------------------------------------------------------------
def test_a_member_at_max_queued_with_parked_sessions_is_refused(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", max_running=1, max_queued=2)
        uid = alice["user_id"]
        _insert(m, "parkedali1", "waiting_approval", owner_id=uid)
        _insert(m, "parkedali2", "waiting_app", owner_id=uid)
        refused = client.post("/api/v1/sessions", json={"prompt": "more"}, headers=H(ALICE))
        assert refused.status_code == 429, refused.text
        # Parked sessions also hold the member's running cap, but not the owner's.
        _insert(m, "queuedali1", "queued", owner_id=uid)
        assert not m._scheduler_eligible("queuedali1")
        m.db.update_session("parkedali1", status="done")
        m.db.update_session("parkedali2", status="done")
        assert m._scheduler_eligible("queuedali1")
        assert client.get("/api/v1/me", headers=H(OWNER)).status_code == 200


# hosted backend slots -----------------------------------------------------------------------------------------------
def test_sessions_parked_on_approval_leave_the_backend_slot_free(tmp_path):
    async def body():
        m, made, _ = _claude_manager(tmp_path, "ask", max_sessions=2)
        await m.start()
        first = m.create("one", backend="claude")["id"]
        second = m.create("two", backend="claude")["id"]
        await wait_status(m, first, "waiting_approval")
        await wait_status(m, second, "waiting_approval")
        third = m.create("three", backend="claude")["id"]
        await wait_status(m, third, "waiting_approval")  # it got a slot and ran up to its own approval
        assert len(made) == 3
        slot = m.runner._backend_slots["claude"]
        await _until(lambda: slot._value == 2)  # all three parked: nobody holds a slot

        for sid, approve in ((first, True), (second, False), (third, True)):
            m.decide(sid, None, approve=approve)
        for sid in (first, second, third):
            await wait_status(m, sid, "done")
        await asyncio.gather(*m.tasks.values())
        assert slot._value == 2  # denied and approved alike took their slot back and gave it up once
        await m.stop()
    asyncio.run(body())


# approval deadline --------------------------------------------------------------------------------------------------
def test_a_hosted_approval_past_its_deadline_is_denied_and_the_run_ends(tmp_path):
    async def body():
        m, _, _ = _claude_manager(tmp_path, "ask", max_sessions=1)
        m.cfg.approval_timeout_seconds = 0.5
        await m.start()
        sid = m.create("run it", backend="claude")["id"]
        s = await wait_status(m, sid, "done", "failed", timeout=30)
        assert (s["status"], s["stop_reason"]) == ("done", "approval_expired")
        approval = m.db.approvals(sid)[0]
        assert approval["status"] == "denied" and "Expired" in approval["note"]
        await asyncio.gather(*m.tasks.values())
        assert m.runner._backend_slots["claude"]._value == 1
        # The slot is free for the next session.
        m.cfg.approval_timeout_seconds = 0
        nxt = m.create("again", backend="claude")["id"]
        await wait_status(m, nxt, "waiting_approval")
        m.decide(nxt, None, approve=True)
        await wait_status(m, nxt, "done")
        await m.stop()
    asyncio.run(body())


def test_a_local_approval_past_its_deadline_is_denied_and_the_run_ends(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    cfg.approval_timeout_seconds = 0.5
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                     Completion(content="never reached")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("try", project="guarded")
        s = await wait_status(m, s["id"], "done", "failed")
        assert (s["status"], s["stop_reason"]) == ("done", "approval_expired")
        assert m.db.approvals(s["id"])[0]["status"] == "denied"
        assert any(e["data"].get("expired") for e in m.db.events(s["id"]) if e["type"] == "approval_decided")
        assert m.scheduler.holder is None
        await m.stop()
    asyncio.run(body())


def test_a_decision_before_the_deadline_stands(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    cfg.approval_timeout_seconds = 3600
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                     Completion(content="wrote it")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("try", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")
        m.decide(s["id"], None, approve=True)
        s = await wait_status(m, s["id"], "done")
        assert s["answer"] == "wrote it"
        await m.stop()
    asyncio.run(body())


# wall-clock budget --------------------------------------------------------------------------------------------------
class SleepyModel:
    """A model that takes a while for every turn and always asks for one more tool call: it never finishes."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.turns = 0

    async def __call__(self, model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0,
                       on_progress=None):
        if tools is None:
            return Completion(content="SUMMARY", prompt_tokens=10, completion_tokens=1)
        self.turns += 1
        await asyncio.sleep(self.seconds)
        return Completion(tool_calls=[call("write_file", self.turns, path=f"f{self.turns}.txt", content="z")],
                          prompt_tokens=10, completion_tokens=1)


def test_a_non_owner_run_stops_on_its_time_budget_and_frees_the_gpu(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 0.6
    model = SleepyModel(0.15)
    m = Manager(cfg, chat=model)
    client = TestClient(create_app(m))
    with client:
        _, shop = _key(client, "shop", "sessions")
        looping = client.post("/api/v1/sessions", headers=shop, json={"prompt": "loop"}).json()["id"]
        wait_for(lambda: _status(client, looping, shop) in ("running", "done"))
        waiting = client.post("/api/v1/sessions", headers=shop, json={"prompt": "next"}).json()["id"]
        wait_for(lambda: _status(client, looping, shop) == "done", timeout=30)
        s = m.db.get_session(looping)
        assert s["stop_reason"] == "budget_time"
        assert s["run"]["turns"] < cfg.max_turns
        # The queued session got the GPU (and, looping too, also stops on its budget).
        wait_for(lambda: m.db.get_session(waiting)["stop_reason"] == "budget_time", timeout=30)


def test_the_owners_run_has_no_time_budget(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 0.01
    m = Manager(cfg, chat=Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                                  Completion(content="finished")]))

    async def body():
        await m.start()
        s = m.create("mine", project="scratch")
        s = await wait_status(m, s["id"], "done")
        assert s["answer"] == "finished" and s["stop_reason"] != "budget_time"
        await m.stop()
    asyncio.run(body())


def test_time_budget_counts_only_time_running(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    m.cfg.max_run_seconds = 100
    app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    app_session = {"id": "s1", "owner_id": OWNER_USER_ID, "app_id": app_id}
    runner = m.runner
    runner._clocks["s1"] = [0.0, None]
    runner._tick_clock("s1", "running")
    runner._clocks["s1"][1] -= 30           # thirty seconds running
    runner._tick_clock("s1", "waiting_approval")
    paused = runner._time_left(app_session)
    assert paused is not None and 69 < paused <= 70
    time.sleep(0.2)                          # time parked does not count
    assert runner._time_left(app_session) == paused
    assert runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": ""}) is None


def test_member_running_cap_still_refuses_with_429_message(tmp_path):
    client, m = household(tmp_path)
    with client:
        alice = create_member(client, ALICE, "Alice", max_queued=1)
        _insert(m, "parkedali3", "waiting_limit", owner_id=alice["user_id"])
        try:
            m._enforce_member_caps(m.db.account_by_id(alice["user_id"]))
        except HarnessError as e:
            assert e.status == 429 and "queued or waiting" in str(e)
        else:
            raise AssertionError("a parked session must count toward max_queued")


# review follow-ups ----------------------------------------------------------------------------------------------------
def test_an_apps_hosted_sessions_waiting_on_a_slot_cannot_both_pass_its_running_cap(tmp_path):
    """Both App sessions wait for a busy backend while the App has nothing running: when slots free up, only one
    may start; the cap check and counting as running happen together."""
    async def body():
        m, made, _ = _claude_manager(tmp_path, "cancel", max_sessions=2)
        await m.start()
        key = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        m.set_app_provider_credential(key["id"], "claude", "", "subscription", [])
        assert m.db.set_app_limits(key["id"], {"max_running": 1})
        owners = [m.create(p, backend="claude")["id"] for p in ("o1", "o2")]
        for sid in owners:
            await wait_status(m, sid, "running")
        apps = [m.create(p, backend="claude", app=key)["id"] for p in ("a1", "a2")]
        await asyncio.sleep(0.2)
        for sid in owners:
            await m.cancel(sid)
        await _until(lambda: any(m.get(sid)["status"] == "running" for sid in apps))
        await asyncio.sleep(0.5)
        assert sorted(m.get(sid)["status"] for sid in apps) == ["queued", "running"]
        assert len(made) == 3
        for sid in apps:
            await m.cancel(sid)
        await m.stop()
    asyncio.run(body())


def test_owner_device_token_sessions_have_no_time_budget(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    device = m.db.create_api_key("phone", "sessions", kind="device")[0]["id"]
    app = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    m.runner._clocks["s1"] = [0.0, None]
    assert m.runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": device}) is None
    assert m.runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": app}) == m.cfg.max_run_seconds
