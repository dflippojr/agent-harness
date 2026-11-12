"""Jobs extraction: isolated databases and fake sessions, never production jobs."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.jobs import settings, service
from test_daemon import make_cfg, Script


def jobs_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.jobs.enabled = True
    return Manager(cfg, chat=Script([Completion(content="fake answer")]))


def test_live_poll_and_enable_check(tmp_path):
    m = jobs_manager(tmp_path)
    specs = {s.key: s for s in settings.specs()}
    assert specs["jobs.enabled"].enable_check(m.cfg) == []
    m.cfg.jobs.poll_seconds = 50
    specs["jobs.poll_seconds"].live_apply(m, 30, 50)
    assert m.jobs.poll_seconds == 50
    settings.apply_jobs_poll(SimpleNamespace(jobs=None), 30, 50)
    m.db.close()


def test_job_and_template_error_paths(tmp_path):
    m = jobs_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        for verb in (client.get, client.delete, client.post):
            path = "/jobs/missing/run" if verb == client.post else "/jobs/missing"
            assert verb(path).status_code == 404
        body = {"name": "Fake", "prompt": "fake", "cron": "@daily"}
        assert client.put("/jobs/missing", json=body).status_code == 404
        jid = client.post("/jobs", json=body).json()["id"]
        assert client.put(f"/jobs/{jid}", json={**body, "cron": "bad"}).status_code == 400
        m.db.update_job(jid, last_session_id="fake-session")
        m._is_active = lambda sid: True
        assert client.post(f"/jobs/{jid}/run").status_code == 409
        assert len(client.get("/jobs/preview", params={"cron": "@daily", "count": 99}).json()["next"]) == 10
        assert len(client.get("/jobs/preview", params={"cron": "@daily", "count": 0}).json()["next"]) == 1
        assert client.put("/templates/missing", json={"name": "x", "prompt": "y"}).status_code == 404
        assert client.delete("/templates/missing").status_code == 404
        for extra in ({"name": " "}, {"prompt": " "}, {"project": "missing"},
                      {"backend": "missing"}, {"model": "missing"}):
            assert client.post("/templates", json={"name": "x", "prompt": "y", **extra}).status_code == 400


@pytest.mark.parametrize("extra", [{"name": ""}, {"prompt": ""}, {"backend": "missing"},
                                   {"model": "missing"}, {"notify": "bad"}])
def test_validate_rejects_invalid_job(extra):
    with pytest.raises(ValueError):
        service.validate({"name": "fake", "prompt": "fake", "cron": "@daily", **extra}, {"scratch": object()}, {})


def test_scheduler_failures_do_not_block_other_jobs(tmp_path):
    m = jobs_manager(tmp_path)
    job = service.validate({"name": "Fake", "prompt": "fake", "cron": "@daily"}, m.cfg.projects, m.cfg.models)
    m.db.insert_job({**job, "id": "fake-job", "next_run_at": 1})
    def fail(*args, **kwargs):
        raise ValueError("fake failure")
    m.jobs.create = fail
    now = time.time()
    assert m.jobs.tick(now) == []
    assert m.db.get_job("fake-job")["last_error"] == "fake failure"
    assert m.db.get_job("fake-job")["next_run_at"] > now
    async def lifecycle():
        m.jobs.start()
        task = m.jobs._task
        m.jobs.start()
        assert m.jobs._task is task
        await m.jobs.stop()
        await m.jobs.stop()
        assert m.jobs._task is None
    asyncio.run(lifecycle())
    m.db.close()


# #933: an empty list item must not widen a cron field to every value.
EMPTY_ITEM_CRONS = ["0,30, * * * *", "0 9 1,,15 * *", "0 9 * * mon,"]


@pytest.mark.parametrize("cron", EMPTY_ITEM_CRONS)
def test_cron_rejects_empty_list_item(cron):
    with pytest.raises(service.CronError):
        service.Cron(cron)


@pytest.mark.parametrize("cron", EMPTY_ITEM_CRONS)
def test_job_routes_reject_empty_list_item(tmp_path, cron):
    m = jobs_manager(tmp_path)
    with TestClient(create_app(m)) as client:
        body = {"name": "Fake", "prompt": "fake", "cron": cron}
        assert client.post("/jobs", json=body).status_code == 400
        jid = client.post("/jobs", json={**body, "cron": "@daily"}).json()["id"]
        assert client.put(f"/jobs/{jid}", json=body).status_code == 400
        assert client.get(f"/jobs/{jid}").json()["cron"] == "0 0 * * *"  # @daily, unchanged
        preview = client.get("/jobs/preview", params={"cron": cron})
        assert preview.status_code == 200 and preview.json()["ok"] is False
    m.db.close()


def test_cron_empty_step_base_stays_wildcard():
    assert service.Cron("/15 * * * *").minutes == {0, 15, 30, 45}


# Class-level check (#933): a list field is exactly the union of its items, and a malformed item never widens it.
# Every field, every list position: a blank item is rejected, and a list of valid items parses to their union.
FIELDS = [  # (index, valid items, parsed attribute)
    (0, ["0", "30", "5-10", "*/20", "45/5"], "minutes"),
    (1, ["0", "9", "1-3", "*/6", "22/1"], "hours"),
    (2, ["1", "15", "28-31", "*/10", "3/9"], "days"),
    (3, ["1", "jan", "mar-may", "*/4", "dec"], "months"),
    (4, ["0", "mon", "tue-thu", "*/2", "7", "sat"], "weekdays"),
]


def _expr(index: int, field: str) -> str:
    parts = ["0", "0", "*", "*", "*"]
    parts[index] = field
    return " ".join(parts)


def _parsed(index: int, field: str, attr: str) -> set[int]:
    return getattr(service.Cron(_expr(index, field)), attr)


@pytest.mark.parametrize("index,items,attr", FIELDS)
def test_cron_list_is_union_of_items_and_rejects_blank_items(index, items, attr):
    import itertools
    for size in (1, 2, 3):
        for combo in itertools.permutations(items, size):
            union = set().union(*(_parsed(index, item, attr) for item in combo))
            assert _parsed(index, ",".join(combo), attr) == union, combo
            for pos in range(size + 1):
                bad = ",".join([*combo[:pos], "", *combo[pos:]])
                with pytest.raises(service.CronError):
                    service.Cron(_expr(index, bad))


def test_scheduler_parks_stored_job_with_empty_list_item(tmp_path):
    """A job saved before #933 with `0,30,` is parked with an error, not run, and doesn't block other jobs."""
    m = jobs_manager(tmp_path)
    good = service.validate({"name": "Good", "prompt": "fake", "cron": "@daily"}, m.cfg.projects, m.cfg.models)
    now = time.time()
    m.db.insert_job({**good, "cron": "0,30, * * * *", "id": "fake-stale", "next_run_at": now - 10})
    m.db.insert_job({**good, "id": "fake-good", "next_run_at": now - 10})
    started = []
    m.jobs.create = lambda *args, **kwargs: started.append(kwargs["job_id"]) or {"id": f"s-{len(started)}"}
    m.jobs._catch_up(now)  # recent misses: catch-up leaves both to tick(), and the stale one must not break it
    assert len(m.jobs.tick(now)) == 1
    assert started == ["fake-good"]
    stale = m.db.get_job("fake-stale")
    assert stale["next_run_at"] is None
    assert "empty list item" in stale["last_error"]
    assert m.jobs.tick(now + 60) == [] and started == ["fake-good"]
    m.db.close()
