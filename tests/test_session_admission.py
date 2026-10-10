"""#524: session admission. No App, member or parked session can hold the shared queue, a hosted backend slot or the
GPU for an unbounded time: per-App caps, parked sessions counted against caps, backend slots released while a session
waits on an approval, an approval deadline, and a wall-clock budget for members' and Apps' runs."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

from fastapi.testclient import TestClient

from harness.api import create_app
from harness.cli_backends import ClaudeSession
from harness.config import BackendConfig
from harness.llm import Completion
from harness.manager import HarnessError, Manager
from harness.principal import OWNER_USER_ID
from harness.runner import HOLDS_PLACE, WAITING, RunClock, unresolved_calls
from harness.storage import workspaces_dir

from test_app_stores import _key
from test_daemon import Script, call, make_cfg, wait_status
from test_household import ALICE, OWNER, H, create_member, household
from test_phase6 import wait_for
from test_phase8 import FAKE_CLAUDE, _claude_manager

LIMITS = "/api/admin/v1/apps/{}/limits"


def _status(client, sid: str, auth: dict) -> str:
    return client.get(f"/api/v1/sessions/{sid}", headers=auth).json()["status"]


def _insert(m, sid: str, status: str, owner_id: str = OWNER_USER_ID, app_id: str = "",
            holds: bool | None = None) -> None:
    """A session row. A running or parked one has run, so it holds its place under its cap unless `holds` says."""
    holds = status in ("running", *WAITING) if holds is None else holds
    now = time.time()
    ws = workspaces_dir(m.cfg, owner_id, app_id) / sid
    ws.mkdir(parents=True, exist_ok=True)
    m.db.insert_session({
        "id": sid, "project": "scratch", "target": "tower", "model": "fake", "backend": "local", "title": sid,
        "status": status, "workspace": str(ws), "created_at": now, "updated_at": now,
        "context": [{"role": "user", "content": "hi"}], "run": {HOLDS_PLACE: True} if holds else {},
        "totals": {}, "inbox": [],
        "owner_id": owner_id, "app_id": app_id,
    })


async def _until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)

class OfflineMac:
    """A runner hub whose Mac is offline until `back()`."""

    def __init__(self):
        self.up = asyncio.Event()

    def online(self, target: str) -> bool:
        return self.up.is_set()

    def startup_grace(self) -> float:
        return 0

    async def wait_online(self, target: str) -> None:
        await self.up.wait()

    def back(self) -> None:
        self.up.set()


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


def test_concurrent_restarts_of_an_apps_finished_sessions_respect_max_queued(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        await m.start()
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        assert m.db.set_app_limits(app_id, {"max_queued": 1})
        m.scheduler.set_paused(True)  # a restarted session stays queued
        _insert(m, "finished01", "done", app_id=app_id)
        _insert(m, "finished02", "done", app_id=app_id)
        results = await asyncio.gather(m.send("finished01", "again"), m.send("finished02", "again"),
                                       return_exceptions=True)
        refused = [r for r in results if isinstance(r, HarnessError)]
        assert len(refused) == 1 and refused[0].status == 429, results
        assert sorted(m.db.get_session(sid)["status"] for sid in ("finished01", "finished02")) == ["done", "queued"]
        m.scheduler.set_paused(False)
        await m.stop()
    asyncio.run(body())


def test_parked_sessions_over_a_lowered_cap_can_still_come_back(tmp_path):
    """Two hosted sessions parked on approvals, then the owner lowers the App's cap to one (and the daemon restarts):
    each already counts, so each may come back to reach its approval; a new one still waits."""
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    assert m.db.set_app_limits(app_id, {"max_running": 1})
    _insert(m, "parked0003", "waiting_approval", app_id=app_id)
    _insert(m, "parked0004", "waiting_approval", app_id=app_id)
    _insert(m, "queued0003", "queued", app_id=app_id)
    assert m._scheduler_eligible("parked0003") and m._scheduler_eligible("parked0004")
    assert not m._scheduler_eligible("queued0003")


def test_local_parked_sessions_over_a_lowered_cap_get_the_gpu_back_once_approved(tmp_path):
    """Two of an App's local sessions wait on approvals and the owner lowers its cap to one: approving either lets
    it take the idle GPU back (it held its place all along), though the other is still parked."""
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                     Completion(content="wrote it")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        first, second = (m.create(p, project="guarded", app=app)["id"] for p in ("one", "two"))
        await wait_status(m, first, "waiting_approval")
        await wait_status(m, second, "waiting_approval")
        assert m.db.set_app_limits(app["id"], {"max_running": 1})
        m.decide(first, None, approve=True)
        s = await wait_status(m, first, "done", timeout=10)
        assert s["answer"] == "wrote it"
        assert m.get(second)["status"] == "waiting_approval"
        m.decide(second, None, approve=True)
        await wait_status(m, second, "done", timeout=10)
        assert m.runner.admitted == {}
        await m.stop()
    asyncio.run(body())

def test_a_session_back_from_an_offline_mac_keeps_its_place_under_the_cap(tmp_path):
    """An App's running Mac session waits for its Mac while another of its sessions is queued for the GPU: when the
    Mac is back the first one gets the GPU again; the other must not take its place meanwhile."""
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        assert m.db.set_app_limits(app_id, {"max_running": 1})
        _insert(m, "macsess001", "running", app_id=app_id)
        m.db.update_session("macsess001", target="macbook")
        _insert(m, "queued0004", "queued", app_id=app_id)
        mac = m.runner.hub = OfflineMac()
        await m.scheduler.acquire("macsess001")
        waiting = asyncio.create_task(m.runner._wait_for_target("macsess001"))
        await _until(lambda: m.db.get_session("macsess001")["status"] == "waiting_target")
        other = asyncio.create_task(m.scheduler.acquire("queued0004"))
        await asyncio.sleep(0.1)
        assert m.scheduler.holder is None and not other.done()  # the parked one still holds the App's slot
        mac.back()
        await asyncio.wait_for(waiting, 5)
        assert m.scheduler.holder == "macsess001" and not other.done()
        assert m.db.get_session("macsess001")["status"] == "running" and m.runner.admitted == {}
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)
    asyncio.run(body())

def test_a_session_whose_running_status_committed_counts_before_the_index_catches_up(tmp_path):
    """The App store has committed a session's `running`, but the index of every store only takes it once the
    commit's callbacks run: the cap must count it already."""
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    assert m.db.set_app_limits(app_id, {"max_running": 1})
    _insert(m, "runsnow001", "queued", app_id=app_id)
    _insert(m, "queued0005", "queued", app_id=app_id)
    with m.db._using(app_id, write=True) as store:  # the row, without the index's callback
        store.update_session("runsnow001", status="running")
    assert not m._scheduler_eligible("queued0005")


def test_a_mac_session_recovered_after_a_restart_keeps_its_place_under_a_lowered_cap(tmp_path):
    """It was running when the daemon stopped and its Mac is offline at the restart: once the Mac is back it takes
    the idle GPU, though another of its App's sessions is parked and the cap is now one."""
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        _insert(m, "macsess002", "running", app_id=app_id)
        m.db.update_session("macsess002", target="macbook")
        _insert(m, "parked0005", "waiting_approval", app_id=app_id)
        assert m.db.set_app_limits(app_id, {"max_running": 1})
        mac = m.runner.hub = OfflineMac()
        waiting = asyncio.create_task(m.runner._wait_for_target("macsess002"))  # the recovered run's setup
        await _until(lambda: m.db.get_session("macsess002")["status"] == "waiting_target")
        mac.back()
        await asyncio.wait_for(waiting, 5)
        await asyncio.wait_for(m.runner._acquire("macsess002"), 5)
        assert m.scheduler.holder == "macsess002" and m.runner.admitted == {}
    asyncio.run(body())

def test_a_run_back_from_a_provider_limit_keeps_its_place_under_a_lowered_cap(tmp_path):
    """Two of an App's hosted runs are parked (one on a provider limit, one on an approval) and the owner lowers
    the cap to one: when the limit resets, that run goes on, with a backend slot free."""
    async def body():
        m, modes, key = _mixed_manager(tmp_path, max_sessions=2)
        await m.start()
        _insert(m, "parked0006", "waiting_approval", app_id=key["id"])
        _insert(m, "limited001", "waiting_limit", app_id=key["id"])
        m.db.update_session("limited001", backend="claude", run={HOLDS_PLACE: True, "limit_resets_at": 0})
        modes["limited001"] = "echo"
        m._spawn("limited001", recovered=True)
        s = await wait_status(m, "limited001", "done", "failed", timeout=15)
        assert (s["status"], s["stop_reason"]) == ("done", "final_message")
        assert m.runner.admitted == {}
        await m.stop()
    asyncio.run(body())

def test_new_sessions_waiting_for_an_offline_mac_hold_no_place_under_the_cap(tmp_path):
    """New sessions held for their offline Mac before they ever ran are not running work: they neither count against
    the App's running cap (blocking its tower sessions) nor skip admission when the Mac is back."""
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        assert m.db.set_app_limits(app_id, {"max_running": 2})
        _insert(m, "running007", "running", app_id=app_id)
        _insert(m, "newmac0001", "queued", app_id=app_id)
        _insert(m, "newmac0002", "queued", app_id=app_id)
        _insert(m, "towerq0001", "queued", app_id=app_id)
        for sid in ("newmac0001", "newmac0002"):
            m.db.update_session(sid, target="macbook")
        m.runner.hub = OfflineMac()
        waits = [asyncio.create_task(m.runner._wait_for_target(sid)) for sid in ("newmac0001", "newmac0002")]
        await _until(lambda: all(m.db.get_session(sid)["status"] == "waiting_target"
                                 for sid in ("newmac0001", "newmac0002")))
        assert m._scheduler_eligible("towerq0001")       # one running of two: the Mac waiters take no place
        _insert(m, "running008", "running", app_id=app_id)
        assert not m._scheduler_eligible("newmac0001")   # at the cap, a never-run Mac session is not let in
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
    asyncio.run(body())


def test_an_approved_session_queued_for_its_slot_keeps_its_place_across_a_restart(tmp_path):
    async def body():
        m, modes, key = _mixed_manager(tmp_path)
        await m.start()
        parked = m.create("asks", backend="claude", app=key)["id"]
        await wait_status(m, parked, "waiting_approval")
        busy = m.create("the owner's", backend="claude")["id"]
        modes[busy] = "cancel"
        await wait_status(m, busy, "running")
        _insert(m, "parked0007", "waiting_approval", app_id=key["id"])  # the App's cap is one: it is over it now
        m.decide(parked, None, approve=True)
        await _until(lambda: m.get(parked)["status"] == "queued")
        await m.stop()                                   # the daemon stops; the place is in the session's run
        restarted = Manager(m.cfg, chat=Script([Completion(content="ok")]))
        assert restarted._scheduler_eligible(parked)
        assert restarted._scheduler_eligible("parked0007")  # parked, it holds its own place too
    asyncio.run(body())

def test_raising_the_default_app_running_cap_lets_a_waiting_session_run_at_once(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.app_max_running = 1

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="ok")]))
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        _insert(m, "parked0008", "waiting_approval", app_id=app["id"])
        sid = m.create("waits", app=app)["id"]
        await asyncio.sleep(0.5)
        assert m.get(sid)["status"] == "queued"
        m.settings.patch_admin({"sessions.app_max_running": 2}, m.settings.admin_view()["revision"])
        s = await wait_status(m, sid, "done", timeout=5)
        assert s["answer"] == "ok"
        await m.stop()
    asyncio.run(body())


def test_a_parked_hosted_run_saved_before_places_were_recorded_recovers_parked(tmp_path):
    """A daemon upgraded with an App's Claude run parked on an approval: the run recovers still waiting on that one
    approval (no duplicate), holding its place, and the decision resumes it."""
    async def body():
        m, modes, key = _mixed_manager(tmp_path)
        await m.start()
        _insert(m, "oldpark001", "waiting_approval", app_id=key["id"], holds=False)
        m.db.update_session("oldpark001", backend="claude")
        m.db.insert_approval({"id": "a-oldpark1", "session_id": "oldpark001", "tool_call_id": "old-call",
                              "tool": "Bash", "args": {"command": "python build.py"}, "reason": "test"})
        m._spawn("oldpark001", recovered=True)
        await _until(lambda: "oldpark001" in m.runner._cli_sessions)
        await asyncio.sleep(0.5)
        s = m.get("oldpark001")
        assert s["status"] == "waiting_approval" and s["run"].get(HOLDS_PLACE)
        assert [a["id"] for a in m.db.approvals("oldpark001")] == ["a-oldpark1"]
        m.decide("oldpark001", None, approve=True)
        await wait_status(m, "oldpark001", "done", timeout=15)
        await m.stop()
    asyncio.run(body())


# members --------------------------------------------------------------------------------------------------------------
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


def test_a_members_parked_session_counts_toward_max_queued(tmp_path):
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


# hosted backend slots -------------------------------------------------------------------------------------------------
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


def _mixed_manager(tmp_path, max_sessions: int = 1):
    """A manager with a fake Claude backend whose mode is picked per session (`modes[sid]`, "ask" by default) and a
    local model that answers at once, plus an App capped at one running session that has a Claude credential."""
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    cfg = make_cfg(tmp_path)
    cfg.backends["claude"] = BackendConfig(enabled=True, model="claude-opus-5", max_sessions=max_sessions)
    m = Manager(cfg, chat=Script([Completion(content="ok")]))
    modes: dict[str, str] = {}

    def factory(**kwargs):
        sid = kwargs["session_id"]
        return ClaudeSession(**kwargs, command=[sys.executable, "-u", str(fake), modes.get(sid, "ask"),
                                                str(tmp_path / f"state-{sid}.jsonl"), "tool-1", "python build.py"])
    m.runner.cli_factory = factory
    key = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
    m.set_app_provider_credential(key["id"], "claude", "", "subscription", [])
    assert m.db.set_app_limits(key["id"], {"max_running": 1})
    return m, modes, key


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


def test_a_session_waiting_on_its_cap_does_not_block_a_parked_one_coming_back(tmp_path):
    """After a restart an App's queued session may reach admission before its parked one: waiting for the parked one
    to end must not keep that one from coming back (and its approval from expiring)."""
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        assert m.db.set_app_limits(app_id, {"max_running": 1})
        _insert(m, "queued0002", "queued", app_id=app_id)
        _insert(m, "parked0002", "waiting_approval", app_id=app_id)
        async def admit(sid):
            async with contextlib.AsyncExitStack() as stack:
                await m.runner._admit_hosted(sid, m.db.get_session(sid), stack)
        waiting = asyncio.create_task(admit("queued0002"))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        await asyncio.wait_for(admit("parked0002"), 2)
        await asyncio.sleep(0.1)
        assert not waiting.done()  # the parked one still holds the App's only running slot
        m.db.update_session("parked0002", status="done")
        m.runner._unreserve("parked0002")
        await asyncio.wait_for(waiting, 2)
    asyncio.run(body())


def test_a_hosted_session_waiting_for_a_backend_slot_holds_its_apps_running_cap(tmp_path):
    """The App's hosted session passed its cap and waits for the busy backend: the App's local session must not
    start meanwhile, or both would run once the backend frees up."""
    async def body():
        m, modes, key = _mixed_manager(tmp_path)
        await m.start()
        busy = m.create("the owner's", backend="claude")["id"]
        modes[busy] = "cancel"
        await wait_status(m, busy, "running")
        hosted = m.create("hosted", backend="claude", app=key)["id"]
        await asyncio.sleep(0.5)
        local = m.create("local", app=key)["id"]
        await asyncio.sleep(0.5)
        assert (m.get(hosted)["status"], m.get(local)["status"]) == ("queued", "queued")
        await m.cancel(busy)
        await wait_status(m, hosted, "waiting_approval")
        assert m.get(local)["status"] == "queued"  # parked, the hosted session still holds the App's slot
        m.decide(hosted, None, approve=True)
        await wait_status(m, hosted, "done")
        await wait_status(m, local, "done")
        await m.stop()
    asyncio.run(body())


def test_an_approved_session_waiting_for_its_backend_slot_keeps_its_place_under_the_cap(tmp_path):
    async def body():
        m, modes, key = _mixed_manager(tmp_path)
        await m.start()
        parked = m.create("asks", backend="claude", app=key)["id"]
        await wait_status(m, parked, "waiting_approval")
        busy = m.create("the owner's", backend="claude")["id"]
        modes[busy] = "cancel"
        await wait_status(m, busy, "running")  # took the slot the parked session gave up
        local = m.create("local", app=key)["id"]
        m.decide(parked, None, approve=True)
        await _until(lambda: m.get(parked)["status"] == "queued")  # approved, waiting for its slot back
        await asyncio.sleep(0.5)
        assert m.get(local)["status"] == "queued"
        await m.cancel(busy)
        await wait_status(m, parked, "done")
        await wait_status(m, local, "done")
        assert m.runner.admitted == {}
        await m.stop()
    asyncio.run(body())


def test_a_cancel_during_a_hosted_app_tool_wait_is_not_undone_by_the_reply(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        _insert(m, "hosted0002", "running", app_id=app_id)
        m.db.update_session("hosted0002", backend="claude")

        class CancelledMeanwhile:
            def names(self, s):
                return {"lookup"}

            async def call(self, s, call_id, name, args, on_wait=None, on_resume=None):
                on_wait()
                await m.runner.aset_status(s["id"], "cancelled", stop_reason="cancelled")
                await on_resume()  # the relay's request ends after the cancel
                return "late answer"
        m.runner.app_tools = CancelledMeanwhile()
        await m.runner._dispatch_mcp(m.db.get_session("hosted0002"), "c1", "lookup", {})
        assert m.db.get_session("hosted0002")["status"] == "cancelled"
    asyncio.run(body())


# approval deadline ----------------------------------------------------------------------------------------------------
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


def test_an_expired_approval_leaves_no_tool_call_unanswered(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    cfg.approval_timeout_seconds = 0.5
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                     Completion(content="never reached")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("try", project="guarded")
        s = await wait_status(m, s["id"], "done", "failed")
        assert s["stop_reason"] == "approval_expired"
        assert unresolved_calls(s["context"]) == []
        await m.stop()
    asyncio.run(body())

def test_an_approval_expires_while_its_recovered_run_waits_for_an_offline_mac(tmp_path):
    """After a restart the run is held for its offline Mac before it reaches the approval: the approval still expires
    on time, is denied, and the run ends with every tool call answered."""
    cfg = make_cfg(tmp_path)
    cfg.approval_timeout_seconds = 1

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="never reached")]))
        m.runner.hub = OfflineMac()
        _insert(m, "macpark001", "waiting_approval")
        pending = call("write_file", 0, path="a.txt", content="x")
        m.db.update_session("macpark001", target="macbook", context=[
            {"role": "user", "content": "write it"}, {"role": "assistant", "content": "", "tool_calls": [pending]}])
        m.db.insert_approval({"id": "a-mac00001", "session_id": "macpark001", "tool_call_id": pending["id"],
                              "tool": "write_file", "args": {"path": "a.txt", "content": "x"}, "reason": "test"})
        await m.start(maintenance=False)
        s = await wait_status(m, "macpark001", "done", "failed", "cancelled", timeout=15)
        assert (s["status"], s["stop_reason"]) == ("done", "approval_expired")
        assert m.db.get_approval("a-mac00001")["status"] == "denied"
        assert unresolved_calls(s["context"]) == []
        await m.stop()
    asyncio.run(body())


# wall-clock budget ----------------------------------------------------------------------------------------------------
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
    runner._clocks["s1"] = RunClock()
    runner._tick_clock("s1", "running")
    runner._clocks["s1"].since -= 30        # thirty seconds running
    runner._tick_clock("s1", "waiting_approval")
    paused = runner._time_left(app_session)
    assert paused is not None and 69 < paused <= 70
    time.sleep(0.2)                          # time parked does not count
    assert runner._time_left(app_session) == paused
    assert runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": ""}) is None


def test_owner_device_token_sessions_have_no_time_budget(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    device = m.db.create_api_key("phone", "sessions", kind="device")[0]["id"]
    app = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    m.runner._clocks["s1"] = RunClock()
    assert m.runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": device}) is None
    assert m.runner._time_left({"id": "s1", "owner_id": OWNER_USER_ID, "app_id": app}) == m.cfg.max_run_seconds


class SlowFinalModel:
    """A model whose one call takes ten seconds and then gives the final answer."""

    async def __call__(self, model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0,
                       on_progress=None):
        await asyncio.sleep(10)
        return Completion(content="finished late", prompt_tokens=10, completion_tokens=1)


def test_the_time_budget_ends_a_run_during_a_long_model_call(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 1

    async def body():
        m = Manager(cfg, chat=SlowFinalModel())
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        started = time.monotonic()
        sid = m.create("slow", app=app)["id"]
        s = await wait_status(m, sid, "done", "failed", timeout=8)
        assert (s["status"], s["stop_reason"]) == ("done", "budget_time")
        assert s["answer"] != "finished late"
        assert time.monotonic() - started < 8
        assert m.scheduler.holder is None
        await m.stop()
    asyncio.run(body())


def test_the_time_budget_ends_a_run_stuck_outside_a_model_call(tmp_path):
    """Compaction (or a tool, or a delegated call) that outlasts the budget is stopped too, not only a model call."""
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 1

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="never asked")]))

        async def slow_compaction(s):
            await asyncio.sleep(10)
            return s
        m.runner._maybe_compact = slow_compaction
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        started = time.monotonic()
        sid = m.create("slow", app=app)["id"]
        s = await wait_status(m, sid, "done", "failed", "cancelled", timeout=8)
        assert (s["status"], s["stop_reason"]) == ("done", "budget_time")
        assert time.monotonic() - started < 8
        assert m.scheduler.holder is None
        await m.stop()
    asyncio.run(body())


def test_time_parked_during_a_model_call_does_not_count(tmp_path):
    """The GPU guard parks a session mid-call (it is queued meanwhile): the budget follows the running clock, so a
    call parked past the budget's wall-clock length still finishes."""
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 1.5
    box: dict = {}

    async def parked_model(model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0,
                           on_progress=None):
        runner, sid = box["runner"], box["sid"]
        await runner.aset_status(sid, "queued")  # stepped aside for the GPU guard
        await asyncio.sleep(2.5)
        await runner.aset_status(sid, "running")
        return Completion(content="done after the pause", prompt_tokens=10, completion_tokens=1)

    async def body():
        m = Manager(cfg, chat=parked_model)
        box["runner"] = m.runner
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        box["sid"] = sid = m.create("pause", app=app)["id"]
        s = await wait_status(m, sid, "done", "failed", timeout=15)
        assert (s["status"], s["stop_reason"], s["answer"]) == ("done", "final_message", "done after the pause")
        await m.stop()
    asyncio.run(body())


def test_a_hosted_sessions_wait_on_an_app_tool_reply_stops_its_clock(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        _insert(m, "hosted0001", "running", app_id=app_id)
        m.db.update_session("hosted0001", backend="claude")
        runner = m.runner
        runner._clocks["hosted0001"] = RunClock(since=time.monotonic())
        seen = {}

        class SlowApp:
            def names(self, s):
                return {"lookup"}

            async def call(self, s, call_id, name, args, on_wait=None, on_resume=None):
                on_wait()
                seen["status"] = m.db.get_session(s["id"])["status"]
                before = runner._time_left(s)
                await asyncio.sleep(0.3)
                seen["paused"] = runner._time_left(s) == before
                await on_resume()
                return "answer"
        runner.app_tools = SlowApp()
        assert await runner._dispatch_mcp(m.db.get_session("hosted0001"), "c1", "lookup", {}) == "answer"
        assert seen == {"status": "waiting_app", "paused": True}
        assert m.db.get_session("hosted0001")["status"] == "running"
    asyncio.run(body())


def test_a_hosted_session_resumes_only_when_its_last_app_call_is_answered(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        _insert(m, "hosted0003", "running", app_id=app_id)
        m.db.update_session("hosted0003", backend="claude")
        replies = {"c1": asyncio.Event(), "c2": asyncio.Event()}
        parked = {"n": 0}

        class TwoCalls:
            def names(self, s):
                return {"lookup"}

            async def call(self, s, call_id, name, args, on_wait=None, on_resume=None):
                on_wait()
                parked["n"] += 1
                await replies[call_id].wait()
                await on_resume()
                return call_id
        m.runner.app_tools = TwoCalls()
        s = m.db.get_session("hosted0003")
        calls = [asyncio.create_task(m.runner._dispatch_mcp(s, cid, "lookup", {})) for cid in ("c1", "c2")]
        await _until(lambda: parked["n"] == 2)
        replies["c1"].set()
        await calls[0]
        assert m.db.get_session("hosted0003")["status"] == "waiting_app"  # c2 still waits on the App
        replies["c2"].set()
        await calls[1]
        assert m.db.get_session("hosted0003")["status"] == "running"
    asyncio.run(body())


def test_a_split_mode_shell_command_is_held_to_the_runs_time_budget(tmp_path):
    m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
    m.cfg.max_run_seconds = 100
    app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
    _insert(m, "split00001", "running", app_id=app_id)
    m.db.update_session("split00001", backend="claude")
    m.runner._clocks["split00001"] = RunClock(spent=95)
    asked = {}

    class Shell:
        def schemas(self):
            return [{"function": {"name": "run_shell", "parameters": {"type": "object", "required": ["command"],
                     "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}}}}]

        async def call(self, name, args):
            asked.update(args)
            return "ok"
    m.runner.split_workspace = lambda s: Shell()
    asyncio.run(m.runner._split_call(m.db.get_session("split00001"), "run_shell", {"command": "sleep 60",
                                                                                     "timeout": 600}))
    assert asked["timeout"] == 5


def test_a_split_mode_runs_end_stops_its_sandbox(tmp_path):
    """A hosted split-mode run's shell commands run in its sandbox: ending the run (a deadline, a cancel) stops it,
    so nothing keeps changing the workspace after the session reports it ended."""
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        _insert(m, "split00002", "done")
        m.db.update_session("split00002", backend="claude")
        stopped = []

        class Box:
            async def stop(self):
                stopped.append(True)
        m.runner._sandboxes["split00002"] = Box()
        await m.runner._end_run("split00002")
        assert stopped == [True]
    asyncio.run(body())


def test_an_app_reply_does_not_restart_the_clock_while_an_approval_is_pending(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="ok")]))
        app_id = m.db.create_api_key("shop", "sessions", kind="app")[0]["id"]
        _insert(m, "hosted0004", "waiting_approval", app_id=app_id)
        m.db.update_session("hosted0004", backend="claude")
        m.db.insert_approval({"id": "a-host0004", "session_id": "hosted0004", "tool_call_id": "t1", "tool": "Bash",
                              "args": {"command": "make"}, "reason": "test"})

        class Lookup:
            def names(self, s):
                return {"lookup"}

            async def call(self, s, call_id, name, args, on_wait=None, on_resume=None):
                on_wait()
                await on_resume()
                return "answer"
        m.runner.app_tools = Lookup()
        await m.runner._dispatch_mcp(m.db.get_session("hosted0004"), "c1", "lookup", {})
        assert m.db.get_session("hosted0004")["status"] == "waiting_approval"  # its clock stays stopped
        m.runner._app_waits["hosted0004"] = 1  # an App call still out when the approval is decided
        m.db.decide_approval("a-host0004", "approved", "")
        assert m.runner._hosted_status("hosted0004") == "waiting_app"
    asyncio.run(body())


def test_a_time_budget_turned_on_mid_run_applies_to_it(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 0
    cfg.approval_timeout_seconds = 0

    async def body():
        m = Manager(cfg, chat=SleepyModel(0.1))
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        sid = m.create("loop", app=app)["id"]
        await wait_status(m, sid, "running")
        m.runner._maybe_compact = _slow_compaction  # the loop never reaches another turn's budget check
        m.settings.patch_admin({"sessions.max_run_seconds": 1}, m.settings.admin_view()["revision"])
        s = await wait_status(m, sid, "done", "failed", timeout=8)
        assert (s["status"], s["stop_reason"]) == ("done", "budget_time")
        await m.stop()
    asyncio.run(body())


async def _slow_compaction(s):
    await asyncio.sleep(60)
    return s


def test_a_run_stopped_on_its_budget_mid_tool_answers_every_tool_call(tmp_path):
    """The next run's context must answer every tool call, or a strict endpoint rejects it."""
    cfg = make_cfg(tmp_path)
    cfg.max_run_seconds = 1
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x"),
                                            call("write_file", 1, path="b.txt", content="y")]),
                     Completion(content="finished")])

    async def body():
        m = Manager(cfg, chat=script)
        real_execute = m.runner._execute

        async def slow_execute(sid, call_, name, args, ws, max_chars=10**9):
            await asyncio.sleep(10)
            return await real_execute(sid, call_, name, args, ws, max_chars)
        m.runner._execute = slow_execute
        await m.start()
        app = m.db.get_api_key(m.db.create_api_key("shop", "sessions", kind="app")[0]["id"])
        sid = m.create("write", app=app)["id"]
        s = await wait_status(m, sid, "done", "failed", timeout=8)
        assert (s["status"], s["stop_reason"]) == ("done", "budget_time")
        assert unresolved_calls(s["context"]) == []
        results = [msg["content"] for msg in s["context"] if msg["role"] == "tool"]
        assert len(results) == 2 and all("time budget" in r for r in results), results
        await m.stop()
    asyncio.run(body())
