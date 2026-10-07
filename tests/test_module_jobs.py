"""Jobs extraction: isolated databases and fake sessions, never production jobs."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.admin import PREFIX
from harness.api import create_app
from harness.llm import Completion
from harness.manager import Manager
from harness.modules import principal_capabilities
from harness_modules.jobs import settings, service
from test_daemon import make_cfg, Script


def jobs_manager(tmp_path, selection="present"):
    cfg = make_cfg(tmp_path)
    cfg.runners = {}
    for section in (cfg.memory_library, cfg.remote_control, cfg.notify, cfg.gpu_guard):
        section.enabled = False
    cfg.jobs.enabled = selection == "present"
    if selection == "absent":
        cfg.module_packages = ["harness_modules.images"]
    if selection == "uninstalled":
        cfg.installed.jobs = False
    return Manager(cfg, chat=Script([Completion(content="fake answer")]))


@pytest.mark.parametrize("selection", ["present", "off", "absent", "uninstalled"])
def test_presence_routes_settings_and_lifecycle(tmp_path, selection):
    m = jobs_manager(tmp_path, selection)
    present = selection in ("present", "off")
    assert (m.modules.get("jobs") is not None) == present
    assert (m.jobs is not None) == (selection == "present")
    assert m.cfg.capabilities()["modules"].get("jobs", False) == (selection == "present")
    caps = principal_capabilities(m.cfg, True)
    assert caps.get("jobs", False) == present
    assert not principal_capabilities(m.cfg, False).get("jobs", False)
    keys = {s.key for s in m.settings.registry.specs.values()}
    assert ("jobs.enabled" in keys) == present
    assert ("jobs.poll_seconds" in keys) == present
    assert any(row[0] == "jobs list" for row in cli.admin_commands())
    with TestClient(create_app(m)) as client:
        assert client.get("/jobs").status_code == (200 if selection == "present" else 400 if present else 404)
        assert client.get(PREFIX + "/templates").status_code == (200 if present else 404)
        assert client.get(PREFIX + "/jobs/preview", params={"cron": "@daily"}).status_code == (200 if present else 404)
        if selection == "present":
            assert m.jobs._task is not None
        if present:
            assert client.post("/templates", json={"name": "Test", "prompt": "fake"}).status_code == 201


def test_settings_accessors_and_live_poll(tmp_path):
    m = jobs_manager(tmp_path)
    specs = {s.key: s for s in settings.specs()}
    enabled, poll = specs["jobs.enabled"], specs["jobs.poll_seconds"]
    assert enabled.getter(m.cfg)
    enabled.setter(m.cfg, False)
    assert not enabled.getter(m.cfg)
    assert enabled.enable_check(m.cfg) == []
    poll.setter(m.cfg, 50)
    assert poll.getter(m.cfg) == 50
    poll.live_apply(m, 30, 50)
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
