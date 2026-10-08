"""Runner._over_quota counts a member's account off the event loop (#464)."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from harness.runner import QUOTA_CHECK_SECONDS, Runner

LIMIT = 100


class Bus:
    def __init__(self):
        self.events = []

    async def aemit(self, sid, kind, payload):
        self.events.append((kind, payload))


class Db:
    def __init__(self, owner_id):
        self.session = {"id": "s1", "owner_id": owner_id, "run": {"workspace_mb": 0}, "target": "tower"}

    def get_session(self, sid):
        return self.session

    def update_session(self, sid, **fields):
        self.session.update(fields)

    def account_by_id(self, uid):
        return {"disk_quota_bytes": LIMIT}


class Ws:
    def __init__(self, nbytes):
        self.nbytes = nbytes

    async def size_bytes(self):
        return self.nbytes


def make_runner(monkeypatch, scan, owner_id="member1", workspace_bytes=0):
    r = object.__new__(Runner)
    r.db, r.bus, r.cfg = Db(owner_id), Bus(), SimpleNamespace()
    r._quota_checked = {}
    r.statuses = []
    r.quota_mb = lambda s: 1000
    r.workspace = lambda s: Ws(workspace_bytes)

    async def aset_status(sid, status, **kw):
        r.statuses.append((status, kw.get("stop_reason")))
    r.aset_status = aset_status
    monkeypatch.setattr("harness.storage.account_usage_bytes", scan)
    return r


@pytest.mark.parametrize("used, over", [(LIMIT - 1, False), (LIMIT, True), (LIMIT + 1, True)])
def test_member_decision_waits_for_the_scan(monkeypatch, used, over):
    r = make_runner(monkeypatch, lambda cfg, uid: used)
    assert asyncio.run(r._over_quota("s1")) is over
    if over:
        assert r.statuses == [("failed", f"account_quota_exceeded: {used} > {LIMIT}")]
        assert r.bus.events[0][0] == "error" and "stopped" in r.bus.events[0][1]["message"]
    else:
        assert r.statuses == [] and r.bus.events == []


def test_scan_runs_off_the_loop_thread(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    seen = {}

    def scan(cfg, uid):
        seen["thread"] = threading.current_thread()
        entered.set()
        assert release.wait(10)
        return LIMIT

    r = make_runner(monkeypatch, scan)

    async def body():
        task = asyncio.ensure_future(r._over_quota("s1"))
        while not entered.is_set():
            await asyncio.sleep(0)
        ran = []

        async def other():
            ran.append(True)
        await asyncio.ensure_future(other())      # the loop is free while the scan is held
        assert ran and not task.done()
        release.set()
        assert await task is True
        assert seen["thread"] is not threading.current_thread()

    asyncio.run(body())


def test_cancel_during_scan_emits_nothing(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def scan(cfg, uid):
        entered.set()
        release.wait(10)
        return LIMIT * 10

    r = make_runner(monkeypatch, scan)

    async def body():
        task = asyncio.ensure_future(r._over_quota("s1"))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(body())
    assert r.statuses == [] and r.bus.events == []


def test_owner_is_exempt_from_the_account_scan(monkeypatch):
    def scan(cfg, uid):
        raise AssertionError("owner must not be scanned")
    r = make_runner(monkeypatch, scan, owner_id=None)
    assert asyncio.run(r._over_quota("s1")) is False


def test_check_cadence_is_kept(monkeypatch):
    calls = []
    r = make_runner(monkeypatch, lambda cfg, uid: calls.append(1) or 0)
    assert asyncio.run(r._over_quota("s1")) is False
    assert asyncio.run(r._over_quota("s1")) is False      # inside QUOTA_CHECK_SECONDS: no second scan
    assert len(calls) == 1 and QUOTA_CHECK_SECONDS == 30
