"""Scheduled jobs and task templates on the owner and admin API."""

import uuid

from fastapi import Request
from pydantic import BaseModel

from harness.modules import HarnessError, RouteTable, manager, operation_audit

NO_SUCH_JOB = "no such job"
owner_routes = RouteTable()


class Job(BaseModel):
    name: str
    prompt: str
    cron: str
    project: str = "scratch"
    backend: str = "local"
    model: str = ""
    notify: str = "low"          # OK results: attention (no notification) | low | always
    enabled: bool = True
    catch_up_minutes: int = 360


class Template(BaseModel):
    name: str
    project: str = "scratch"
    backend: str = "local"
    model: str = ""
    prompt: str


# scheduled jobs (jobs.py)
def jobs_on(request: Request):
    m = manager(request)
    if m.jobs is None:
        raise HarnessError(400, "scheduled jobs are disabled in config/harness.yaml")
    return m


def job_view(m, job: dict, runs: int = 1) -> dict:
    recent = m.db.job_sessions(job["id"], limit=runs)
    return {**job, "enabled": bool(job["enabled"]), "recent": recent}


@owner_routes.get("/jobs")
async def list_jobs(request: Request):
    m = jobs_on(request)
    if request.state.access.role == "guest":
        return []
    return [job_view(m, j) for j in m.db.list_jobs()]


@owner_routes.get("/jobs/preview")
async def preview_cron(cron: str, request: Request, count: int = 3):
    """The next few run times of a schedule, or why it's invalid."""
    import time as _time
    from .service import Cron, CronError
    try:
        c = Cron(cron)
    except CronError as e:
        return {"ok": False, "error": str(e)}
    times, t = [], _time.time()
    for _ in range(max(1, min(count, 10))):
        t = c.next_after(t)
        times.append(t)
    return {"ok": True, "cron": c.expr, "next": times}


@owner_routes.post("/jobs", status_code=201)
async def create_job(body: Job, request: Request):
    import time as _time
    from .service import Cron, new_job_id, validate
    m = jobs_on(request)
    try:
        job = validate(body.model_dump(), m.cfg.projects, m.cfg.models, m.cfg.backends)
    except ValueError as e:
        raise HarnessError(400, str(e))
    job["id"] = new_job_id()
    job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
    context = operation_audit.request_context(request, m)
    def save():
        m.db.insert_job(job)
        operation_audit.append(m.db, context, job["id"], "job.create", "ok",
                               {"fields": list(body.model_fields), "enabled": job["enabled"]})
    m.db.write(save)
    return job_view(m, m.db.get_job(job["id"]))


@owner_routes.get("/jobs/{jid}")
async def get_job(jid: str, request: Request):
    m = jobs_on(request)
    if request.state.access.role == "guest":
        raise HarnessError(404, NO_SUCH_JOB)
    job = m.db.get_job(jid)
    if job is None:
        raise HarnessError(404, NO_SUCH_JOB)
    return job_view(m, job, runs=15)


@owner_routes.put("/jobs/{jid}")
async def update_job(jid: str, body: Job, request: Request):
    import time as _time
    from .service import Cron, validate
    m = jobs_on(request)
    old = m.db.get_job(jid)
    if old is None:
        raise HarnessError(404, NO_SUCH_JOB)
    try:
        job = validate(body.model_dump(), m.cfg.projects, m.cfg.models, m.cfg.backends)
    except ValueError as e:
        raise HarnessError(400, str(e))
    job["next_run_at"] = Cron(job["cron"]).next_after(_time.time())
    context = operation_audit.request_context(request, m)
    def save():
        m.db.update_job(jid, **job)
        operation_audit.append(m.db, context, jid, "job.update", "ok",
                               {"fields": [k for k in body.model_fields if old.get(k) != job.get(k)],
                                "enabled": job["enabled"]})
    m.db.write(save)
    return job_view(m, m.db.get_job(jid), runs=15)


@owner_routes.delete("/jobs/{jid}", status_code=204)
async def delete_job(jid: str, request: Request):
    m = jobs_on(request)
    context = operation_audit.request_context(request, m)
    def remove():
        if not m.db.delete_job(jid):
            raise HarnessError(404, NO_SUCH_JOB)
        operation_audit.append(m.db, context, jid, "job.delete", "ok")
    m.db.write(remove)


@owner_routes.post("/jobs/{jid}/run", status_code=201)
async def run_job(jid: str, request: Request):
    """Run a job now, outside its schedule (the next scheduled run is unchanged)."""
    m = jobs_on(request)
    job = m.db.get_job(jid)
    if job is None:
        raise HarnessError(404, NO_SUCH_JOB)
    if job["last_session_id"] and m._is_active(job["last_session_id"]):
        raise HarnessError(409, "the previous run is still going")
    sid = m.jobs.run(job, manual=True, context=operation_audit.request_context(request, m))
    return m.summary(m.db.get_session(sid))


# templates
@owner_routes.get("/templates")
async def list_templates(request: Request):
    if request.state.access.role == "guest":
        return []
    return manager(request).db.list_templates()


@owner_routes.post("/templates", status_code=201)
async def create_template(body: Template, request: Request):
    return _save_template(manager(request), "t-" + uuid.uuid4().hex[:8], body)


@owner_routes.put("/templates/{tid}")
async def update_template(tid: str, body: Template, request: Request):
    m = manager(request)
    if m.db.get_template(tid) is None:
        raise HarnessError(404, "no such template")
    return _save_template(m, tid, body)


@owner_routes.delete("/templates/{tid}", status_code=204)
async def delete_template(tid: str, request: Request):
    if not manager(request).db.delete_template(tid):
        raise HarnessError(404, "no such template")


def _save_template(m, tid: str, body: Template) -> dict:
    if not body.name.strip() or not body.prompt.strip():
        raise HarnessError(400, "name and prompt are required")
    if body.project not in m.cfg.projects:
        raise HarnessError(400, f"unknown project {body.project!r}")
    if body.backend != "local" and (body.backend not in m.cfg.backends or not m.cfg.backends[body.backend].enabled):
        raise HarnessError(400, f"unknown or disabled backend {body.backend!r}")
    if body.backend == "local" and body.model and body.model not in m.cfg.models:
        raise HarnessError(400, f"unknown model {body.model!r}")
    m.db.upsert_template({"id": tid, **body.model_dump()})
    return m.db.get_template(tid)
