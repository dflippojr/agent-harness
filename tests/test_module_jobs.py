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
