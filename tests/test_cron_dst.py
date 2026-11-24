"""Cron schedules across daylight-saving changes (#932), with local time pinned to zones that change their clocks.

Job times are local wall-clock times. When clocks fall back, an hour of wall-clock times happens twice; when they
spring forward, an hour never happens. `next_after(t)` must still always land strictly after `t`, and a fixed-time
job runs once per wall-clock slot (vixie-cron: at the first pass, and at the second pass only for a job created or
re-planned during the second pass). Jobs with `*` in the minute or hour field keep their real-time rhythm through
both passes.

The grid tests are the class-level check: every minute around every 2026 transition in three zones (one with a
30-minute shift), for sub-hourly, hourly and daily expressions.
"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness_modules.jobs import service
from harness_modules.jobs.service import Cron, JobScheduler
from harness_modules.jobs.store import JobStore
from test_module_jobs import jobs_manager

ZONES = ["America/New_York", "Europe/London", "Australia/Lord_Howe"]  # Lord Howe shifts by 30 minutes
WILDCARD = ["*/5 * * * *", "0 * * * *", "*/5 1 * * *"]
FIXED = ["45 1 * * *", "30 2 * * *", "50 1 * * *", "15 1,2 * * *"]
# Every test runs with the zone pinned through zoneinfo (any OS), and also through the process's TZ, the path the
# daemon uses, where the OS lets a process change its zone (time.tzset: not on Windows).
CLOCKS = ["zoneinfo"] + (["TZ"] if hasattr(time, "tzset") else [])


@pytest.fixture(params=CLOCKS)
def tz(request, monkeypatch):
    """Pin local time to a zone; restore it afterwards so later tests on this worker are unaffected."""
    def pin(zone: str) -> None:
        if request.param == "TZ":
            monkeypatch.setenv("TZ", zone)
            time.tzset()
        else:
            monkeypatch.setattr(service, "LOCAL", service.ZoneClock(ZoneInfo(zone)))
    yield pin
    monkeypatch.undo()
    if request.param == "TZ":
        time.tzset()


def utc(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def show(t: float) -> str:
    return f"{datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC ({service.LOCAL.local(t):%H:%M} local)"


def transitions(year: int = 2026) -> list[float]:
    """The instants in `year` at which the local UTC offset changes (to the minute)."""
    offset = service.LOCAL.offset
    found, t = [], utc(year, 1, 1)
    end = utc(year + 1, 1, 1)
    while t < end:
        if offset(t) != offset(t + 3600):
            lo, hi = t, t + 3600  # offset differs between lo and hi: narrow to the minute
            while hi - lo > 60:
                mid = lo + (hi - lo) // 120 * 60
                lo, hi = (mid, hi) if offset(mid) == offset(lo) else (lo, mid)
            found.append(hi)
        t += 3600
    return found


def wall(t: float) -> tuple:
    d = service.LOCAL.local(t)
    return d.year, d.month, d.day, d.hour, d.minute


# --- the issue's examples -----------------------------------------------------------------------------------------

def test_issue_examples_land_after_t(tz):
    tz("America/New_York")
    t = utc(2026, 11, 1, 6, 30)  # 01:30 EST, the second pass
    assert Cron("45 1 * * *").next_after(t) == utc(2026, 11, 1, 6, 45), "01:45 EST, not 01:45 EDT"
    assert Cron("*/5 * * * *").next_after(t) == utc(2026, 11, 1, 6, 35), "01:35 EST, not 01:35 EDT"


def test_fixed_job_runs_at_the_first_pass_only(tz):
    tz("America/New_York")
    cron = Cron("45 1 * * *")
    assert cron.next_after(utc(2026, 11, 1, 5, 0)) == utc(2026, 11, 1, 5, 45)  # 01:00 EDT -> 01:45 EDT
    after_run = cron.next_after(utc(2026, 11, 1, 5, 45, 20))  # it just ran at 01:45 EDT
    assert after_run == utc(2026, 11, 2, 6, 45), show(after_run)  # 01:45 EST the next day, not 01:45 EST today


def test_wildcard_job_keeps_running_through_the_second_pass(tz):
    tz("America/New_York")
    assert Cron("*/5 * * * *").next_after(utc(2026, 11, 1, 5, 55)) == utc(2026, 11, 1, 6, 0)  # 01:55 EDT -> 01:00 EST


def test_next_slot_skips_a_slot_whose_first_pass_the_job_existed_for(tz):
    tz("America/New_York")
    now = utc(2026, 11, 1, 6, 30)  # re-planned at 01:30 EST
    old, late = utc(2026, 10, 1), utc(2026, 11, 1, 5, 50)  # created a month ago / after 01:35 EDT had passed
    assert Cron("35 1 * * *").next_slot(now, old) == utc(2026, 11, 2, 6, 35)
    assert Cron("35 1 * * *").next_slot(now, None) == utc(2026, 11, 2, 6, 35)
    assert Cron("*/5 * * * *").next_slot(now, old) == utc(2026, 11, 1, 6, 35)  # wildcard: 01:35 EST runs
    assert Cron("35 1 * * *").next_slot(now, late) == utc(2026, 11, 1, 6, 35)  # missed 01:35 EDT: 01:35 EST
    assert Cron("15,45 1 * * *").next_slot(utc(2026, 11, 1, 6, 0), old) == utc(2026, 11, 2, 6, 15)
    assert Cron("15,45 1 * * *").next_slot(utc(2026, 11, 1, 6, 0), utc(2026, 11, 1, 5, 20)) == utc(2026, 11, 1, 6, 15)


def test_wildcard_second_pass_found_when_the_next_match_is_a_year_away(tz):
    tz("America/New_York")
    # From 01:55 EDT the next first-pass match is a year later (also EDT), but 01:00 EST today matches.
    assert Cron("*/5 1 1 11 *").next_after(utc(2026, 11, 1, 5, 55)) == utc(2026, 11, 1, 6, 0)


def test_wildcard_next_after_months_away_is_quick(tz):
    tz("America/New_York")
    begin = time.perf_counter()
    assert Cron("0 * * 1 *").next_after(utc(2026, 7, 1)) == utc(2027, 1, 1, 5, 0)
    assert time.perf_counter() - begin < 0.5


def test_spring_forward_gap_still_runs_once(tz):
    tz("America/New_York")
    # 02:30 doesn't exist on 2026-03-08; the job runs once that day, after the gap (as before #932).
    nxt = Cron("30 2 * * *").next_after(utc(2026, 3, 8, 6, 30))  # 01:30 EST
    assert nxt == utc(2026, 3, 8, 7, 30), show(nxt)  # 03:30 EDT
    assert Cron("30 2 * * *").next_after(nxt) == utc(2026, 3, 9, 6, 30)  # 02:30 EDT the next day


# --- class-level check: every minute around every transition ------------------------------------------------------

@pytest.mark.parametrize("zone", ZONES)
def test_next_after_is_strictly_after_t_around_every_transition(tz, zone):
    tz(zone)
    edges = transitions()
    assert len(edges) == 2, edges
    bad = []
    for edge in edges:
        for minute in range(-180, 181):
            for second in (0, 30):
                t = edge + minute * 60 + second
                for expr in WILDCARD + FIXED:
                    nxt = Cron(expr).next_after(t)
                    if not t < nxt <= t + 2 * 86400 or nxt % 60:
                        bad.append(f"{expr!r}: next_after({show(t)}) = {show(nxt)}")
    assert not bad, f"{len(bad)} bad results, first: {bad[:5]}"


def chain(cron: Cron, start: float, end: float) -> list[float]:
    runs, t = [], start
    while True:
        t = cron.next_after(t)
        if t >= end:
            return runs
        runs.append(t)


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("expr", FIXED)
def test_fixed_jobs_run_once_per_wall_clock_slot(tz, zone, expr):
    tz(zone)
    cron = Cron(expr)
    for edge in transitions():
        runs = chain(cron, edge - 2 * 86400, edge + 2 * 86400)
        assert runs == sorted(set(runs)), [show(r) for r in runs]
        slots = [wall(r) for r in runs]
        assert len(slots) == len(set(slots)), f"a wall-clock slot ran twice: {[show(r) for r in runs]}"
        days = {slot[:3] for slot in slots}
        around = {wall(edge + k * 86400)[:3] for k in (-1, 0, 1)}
        assert around <= days, [show(r) for r in runs]  # every day runs, the day of the transition too


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("expr", WILDCARD)
def test_wildcard_jobs_match_every_real_minute_once(tz, zone, expr):
    """Walking next_after across a transition gives exactly the real minutes whose local time matches."""
    tz(zone)
    cron = Cron(expr)
    for edge in transitions():
        start, end = edge - 3 * 3600, edge + 3 * 3600
        expected = [m for m in range(int(start) + 60, int(end), 60)
                    if wall(m)[4] in cron.minutes and wall(m)[3] in cron.hours]
        assert chain(cron, start, end) == expected


# --- the scheduler, ticking every 30 seconds through the night ----------------------------------------------------

def run_night(tz, tmp_path, jobs: dict[str, dict], start: float, end: float, catch_up: bool = False,
              final: dict | None = None) -> dict[str, list[float]]:
    """Insert `jobs` (id -> fields), tick every 30 s from start to end with sessions that finish at once; return
    the session start times per job."""
    tz("America/New_York")
    m = jobs_manager(tmp_path)
    started: dict[str, list[float]] = {jid: [] for jid in jobs}
    clock = {"now": start}

    def create(prompt, **kw):
        started[kw["job_id"]].append(clock["now"])
        return {"id": f"s-{kw['job_id']}-{len(started[kw['job_id']])}"}

    scheduler = JobScheduler(m.db, create, lambda _sid: False)
    store = JobStore(m.db)
    for jid, fields in jobs.items():
        created_at = fields.pop("created_at", start)
        store.insert_job({"id": jid, "name": jid, "prompt": "p", "project": "scratch", "backend": "local",
                          "model": "", "notify": "low", "enabled": True, "catch_up_minutes": 360,
                          "next_run_at": Cron(fields["cron"]).next_after(created_at), **fields})
        store.update_job(jid, created_at=created_at)
    if catch_up:
        scheduler._catch_up(start)  # the daemon starts at `start`
    while clock["now"] < end:
        scheduler.tick(clock["now"])
        clock["now"] += 30
    if final is not None:
        final.update({jid: store.get_job(jid) for jid in jobs})
    m.db.close()
    return started


def test_scheduler_runs_a_fixed_job_once_in_the_repeated_hour(tz, tmp_path):
    started = run_night(tz, tmp_path, {"daily": {"cron": "45 1 * * *"}},
                        utc(2026, 11, 1, 4, 0), utc(2026, 11, 1, 8, 0))  # 00:00 EDT .. 03:00 EST
    assert started["daily"] == [utc(2026, 11, 1, 5, 45)], [show(t) for t in started["daily"]]  # 01:45 EDT only


def test_scheduler_runs_a_job_created_in_the_second_pass_at_its_time(tz, tmp_path):
    started = run_night(tz, tmp_path, {"daily": {"cron": "45 1 * * *", "created_at": utc(2026, 11, 1, 6, 30)}},
                        utc(2026, 11, 1, 6, 30), utc(2026, 11, 1, 8, 0))  # created 01:30 EST
    assert started["daily"] == [utc(2026, 11, 1, 6, 45)], [show(t) for t in started["daily"]]  # 01:45 EST, not early


def test_scheduler_running_a_slot_late_in_the_second_pass_does_not_run_its_twin(tz, tmp_path):
    """Planned for 01:45 EDT, but the daemon only starts at 01:30 EST: the catch-up run is that slot, so 01:45 EST
    must not run it again."""
    final: dict = {}
    started = run_night(tz, tmp_path, {"daily": {"cron": "45 1 * * *", "created_at": utc(2026, 11, 1, 5, 0)}},
                        utc(2026, 11, 1, 6, 30), utc(2026, 11, 1, 8, 0), catch_up=True, final=final)
    assert started["daily"] == [utc(2026, 11, 1, 6, 30)], [show(t) for t in started["daily"]]
    assert final["daily"]["next_run_at"] == utc(2026, 11, 2, 6, 45)


def test_scheduler_runs_a_sub_hourly_job_once_per_slot(tz, tmp_path):
    start, end = utc(2026, 11, 1, 4, 0), utc(2026, 11, 1, 8, 0)
    started = run_night(tz, tmp_path, {"five": {"cron": "*/5 * * * *"}}, start, end)
    assert started["five"] == [start + 300 * k for k in range(1, 48)], [show(t) for t in started["five"]]


def test_editing_a_job_in_the_second_pass_does_not_rerun_its_slot(tz, tmp_path, monkeypatch):
    """The job ran at 01:45 EDT; saving it again at 01:30 EST must not schedule 01:45 EST."""
    tz("America/New_York")
    m = jobs_manager(tmp_path)
    fields = {"name": "Nightly", "prompt": "check", "cron": "45 1 * * *", "project": "scratch"}
    with TestClient(create_app(m)) as client:
        monkeypatch.setattr(time, "time", lambda: utc(2026, 11, 1, 5, 0))  # 01:00 EDT
        job = client.post("/jobs", json=fields).json()
        assert job["next_run_at"] == utc(2026, 11, 1, 5, 45)
        JobStore(m.db).update_job(job["id"], last_run_at=utc(2026, 11, 1, 5, 45, 5),
                                  next_run_at=utc(2026, 11, 2, 6, 45))
        monkeypatch.setattr(time, "time", lambda: utc(2026, 11, 1, 6, 30))  # 01:30 EST
        edited = client.put(f"/jobs/{job['id']}", json={**fields, "prompt": "check more"}).json()
    monkeypatch.undo()
    assert edited["next_run_at"] == utc(2026, 11, 2, 6, 45), show(edited["next_run_at"])
    m.db.close()


@pytest.mark.parametrize("cron,ran,expected", [
    ("45 1 * * *", [utc(2026, 11, 1, 5, 46)], utc(2026, 11, 2, 6, 45)),  # 01:45 EDT slot started late, at 01:46
    ("15,45 1 * * *", [utc(2026, 11, 1, 5, 15), utc(2026, 11, 1, 5, 45)], utc(2026, 11, 2, 6, 15)),  # both slots
])
def test_editing_in_the_second_pass_never_reruns_a_first_pass_slot(tz, tmp_path, monkeypatch, cron, ran, expected):
    """Saving the job at 01:00 EST: every slot of the first pass was served, however late it started."""
    tz("America/New_York")
    m = jobs_manager(tmp_path)
    fields = {"name": "Nightly", "prompt": "check", "cron": cron, "project": "scratch"}
    with TestClient(create_app(m)) as client:
        monkeypatch.setattr(time, "time", lambda: utc(2026, 11, 1, 4, 0))  # created 00:00 EDT
        job = client.post("/jobs", json=fields).json()
        JobStore(m.db).update_job(job["id"], last_run_at=ran[-1], next_run_at=expected)
        monkeypatch.setattr(time, "time", lambda: utc(2026, 11, 1, 6, 0))  # 01:00 EST
        edited = client.put(f"/jobs/{job['id']}", json={**fields, "prompt": "check more"}).json()
    monkeypatch.undo()
    assert edited["next_run_at"] == expected, show(edited["next_run_at"])
    m.db.close()


def test_a_job_that_never_ran_edited_in_the_second_pass_runs_at_its_time(tz, tmp_path, monkeypatch):
    tz("America/New_York")
    m = jobs_manager(tmp_path)
    fields = {"name": "Nightly", "prompt": "check", "cron": "45 1 * * *", "project": "scratch"}
    with TestClient(create_app(m)) as client:
        monkeypatch.setattr(time, "time", lambda: utc(2026, 11, 1, 6, 30))  # 01:30 EST
        job = client.post("/jobs", json=fields).json()
        edited = client.put(f"/jobs/{job['id']}", json={**fields, "prompt": "check more"}).json()
    monkeypatch.undo()
    assert job["next_run_at"] == edited["next_run_at"] == utc(2026, 11, 1, 6, 45)
    m.db.close()


def test_dst_transitions_found(tz):
    """The helper finds New York's 2026 changes (guards the grid tests against testing nothing)."""
    tz("America/New_York")
    assert transitions() == [utc(2026, 3, 8, 7, 0), utc(2026, 11, 1, 6, 0)]
