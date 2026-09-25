"""Scheduled jobs: recurring agent tasks on a cron schedule (Phase 7d).

A job is a prompt, a project and a model plus a five-field cron expression in the tower's local time
(minute hour day-of-month month day-of-week; `*`, lists, ranges, steps, and names like `mon-fri`). The daemon checks
every 30 s and starts an ordinary session for each due job, so jobs queue for the GPU, ask for approvals, and pause
for games like any other task.

Notifications (user decision: quiet unless attention): the job prompt asks the agent to end its answer with
`STATUS: OK` or `STATUS: ATTENTION`. ATTENTION (or a missing status line, or a failed run) notifies like a normal
task; OK sends a low-priority notification, or none when the job's `notify` is `attention`. Approval requests always
notify.

Runs don't pile up: a job whose previous session is still active skips that slot. After downtime, a job whose last
slot was missed by less than `catch_up_minutes` runs once when the daemon starts; older misses are skipped.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("harness.jobs")

NOTIFY_MODES = ("attention", "low", "always")  # OK results: no notification / low priority / normal priority
STATUS_PROMPT = ("This is a scheduled job, so the user isn't watching, and an OK result is barely shown to them. Do the "
                 "check, then end your final answer with one last line: `STATUS: OK` only if everything the task "
                 "expects is true, or `STATUS: ATTENTION: <one-line reason>` if anything isn't, even when there may be "
                 "a harmless explanation (say what it might be; the user decides). Don't make changes that need "
                 "approval unless the task says to.")
_STATUS_HEAD = re.compile(r"STATUS:\s*(OK|ATTENTION)\b", re.IGNORECASE)
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
MONTH_NAMES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                           "nov", "dec"], 1)}
PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    values: set[int] = set()
    for part in text.lower().split(","):
        values.update(_field_part(part, text, lo, hi, names))
    return values


def _field_part(part: str, text: str, lo: int, hi: int, names: dict | None) -> range:
    step = 1
    if "/" in part:
        part, step_text = part.split("/", 1)
        if not step_text.isdigit() or int(step_text) < 1:
            raise CronError(f"bad step in {text!r}")
        step = int(step_text)
    if part in ("*", ""):
        start, end = lo, (6 if hi == 7 else hi)  # * in day-of-week: 0-6
    elif "-" in part:
        a, b = part.split("-", 1)
        start, end = _value(a, lo, hi, names), _value(b, lo, hi, names)
    else:
        start = _value(part, lo, hi, names)
        end = hi if step > 1 else start
    if start > end:
        raise CronError(f"range {part!r} runs backwards")
    return range(start, end + 1, step)


def _value(text: str, lo: int, hi: int, names: dict | None) -> int:
    if names and text[:3] in names:
        return names[text[:3]]
    if not text.isdigit():
        raise CronError(f"{text!r} isn't a number")
    n = int(text)
    if not lo <= n <= hi:
        raise CronError(f"{n} is outside {lo}-{hi}")
    return n


class Cron:
    def __init__(self, expr: str):
        self.expr = PRESETS.get(expr.strip().lower(), expr.strip())
        parts = self.expr.split()
        if len(parts) != 5:
            raise CronError("a schedule needs 5 fields: minute hour day-of-month month day-of-week")
        self.minutes = _field(parts[0], 0, 59)
        self.hours = _field(parts[1], 0, 23)
        self.days = _field(parts[2], 1, 31)
        self.months = _field(parts[3], 1, 12, MONTH_NAMES)
        self.weekdays = {d % 7 for d in _field(parts[4], 0, 7, DOW_NAMES)}  # 0 and 7 are both Sunday
        # Standard cron: when both day fields are restricted, either one matching is enough.
        self.day_any = parts[2] != "*" and parts[4] != "*"

    def _day_ok(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.weekday() + 1) % 7 in self.weekdays
        if self.day_any:
            return dom or dow
        return dom and dow

    def next_after(self, t: float) -> float:
        """The first matching minute strictly after timestamp t (local time)."""
        d = datetime.fromtimestamp(t).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = d + timedelta(days=366 * 5)
        while d < limit:
            if d.month not in self.months:
                d = (d.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(d):
                d = d.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if d.hour not in self.hours:
                d = d.replace(minute=0) + timedelta(hours=1)
                continue
            if d.minute not in self.minutes:
                d += timedelta(minutes=1)
                continue
            return d.timestamp()
        raise CronError(f"{self.expr!r} never matches")

    def describe(self) -> str:
        return self.expr


def _line_start_before(text: str, k: int, floor: int) -> int | None:
    """The first line start at or after floor from which only non-word characters lead up to k, if any."""
    r = k
    while r > floor and not (text[r - 1].isalnum() or text[r - 1] == "_"):
        r -= 1
    if r == 0 or text[r - 1] == "\n":
        return r
    newline = text.find("\n", r, k)
    return None if newline < 0 else newline + 1


def _status_lines(text: str):
    """(start, end, verdict, reason) for each STATUS line, the matches of the multiline, case-insensitive
    `^\\W*STATUS:\\s*(OK|ATTENTION)\\b[:\\s-]*(.*)$`. Scanned by hand: that regex retries a run of non-word
    characters from every line start inside it, which is quadratic."""
    pos = floor = 0
    while m := _STATUS_HEAD.search(text, pos):
        start = _line_start_before(text, m.start(), floor)
        if start is None:
            pos = m.start() + 1
            continue
        s = m.end()
        while s < len(text) and (text[s] in ":-" or text[s].isspace()):
            s += 1
        end = text.find("\n", s)
        end = len(text) if end < 0 else end
        yield start, end, m.group(1), text[s:end]
        pos = floor = end


def parse_status(answer: str) -> tuple[str, str]:
    """('ok' | 'attention' | '', reason) from a job's final answer. The last STATUS line wins."""
    matches = list(_status_lines(answer or ""))
    if not matches:
        return "", ""
    _, _, verdict, reason = matches[-1]
    return verdict.lower(), reason.strip().rstrip("*_` ").strip()


def _strip_status_lines(text: str) -> str:
    parts, pos = [], 0
    for start, end, _, _ in _status_lines(text):
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def summary(answer: str, limit: int = 300) -> str:
    """A notification-sized summary of a job's answer: the last prose paragraph before the STATUS line (agents
    usually put their verdict there), skipping tables, headings and code."""
    text = _strip_status_lines(answer or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for p in reversed(paragraphs):
        lines = [line for line in p.splitlines() if not re.match(r"\s*(\||#|```|---)", line)]
        prose = " ".join(" ".join(lines).split())
        if len(prose) >= 20:
            return prose if len(prose) <= limit else prose[: limit - 1] + "…"
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _check_backend(backend: str, model: str, models: dict, backends: dict | None) -> None:
    if backend != "local" and (backend not in (backends or {}) or not backends[backend].enabled):
        raise ValueError(f"unknown or disabled backend {backend!r}")
    if backend == "local" and model and model not in models:
        raise ValueError(f"unknown model {model!r}")


def validate(job: dict, projects: dict, models: dict, backends: dict | None = None) -> dict:
    name = (job.get("name") or "").strip()
    prompt = (job.get("prompt") or "").strip()
    if not name or not prompt:
        raise ValueError("name and prompt are required")
    cron = Cron(job.get("cron") or "")
    project = job.get("project") or "scratch"
    if project not in projects:
        raise ValueError(f"unknown project {project!r}")
    model = job.get("model") or ""
    backend = job.get("backend") or "local"
    _check_backend(backend, model, models, backends)
    notify = job.get("notify") or "low"
    if notify not in NOTIFY_MODES:
        raise ValueError(f"notify must be one of {', '.join(NOTIFY_MODES)}")
    return {"name": name[:80], "prompt": prompt, "cron": cron.expr, "project": project, "backend": backend,
            "model": model,
            "notify": notify, "enabled": bool(job.get("enabled", True)),
            "catch_up_minutes": max(0, int(job.get("catch_up_minutes", 360)))}


class JobScheduler:
    """Starts sessions for due jobs. `create` is Manager.create; `active` tells whether a session is still running."""

    def __init__(self, db, create, active, poll_seconds: float = 30):
        self.db = db
        self.create = create
        self.active = active
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._catch_up(time.time())
            self._task = asyncio.create_task(self._loop(), name="jobs")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                self.tick(time.time())
            except Exception:  # noqa: BLE001 - keep scheduling
                log.exception("job tick failed")
            await asyncio.sleep(self.poll_seconds)

    def _catch_up(self, now: float) -> None:
        """At start: jobs whose slot passed while the daemon was down run once if the miss is recent, else skip."""
        for job in self.db.list_jobs():
            if not job["enabled"] or not job["next_run_at"] or job["next_run_at"] > now:
                continue
            if now - job["next_run_at"] <= job["catch_up_minutes"] * 60:
                continue  # tick() runs it
            self._reschedule(job, now, skipped="missed while the tower or daemon was down")

    def tick(self, now: float) -> list[str]:
        started = []
        for job in self.db.list_jobs():
            if not job["enabled"] or not job["next_run_at"] or job["next_run_at"] > now:
                continue
            if job["last_session_id"] and self.active(job["last_session_id"]):
                self._reschedule(job, now, skipped="the previous run was still going")
                continue
            try:
                sid = self.run(job, now)
                started.append(sid)
            except Exception as e:  # noqa: BLE001 - a broken job must not block the others
                log.warning("job %s could not start: %s", job["id"], e)
                self._reschedule(job, now, error=str(e)[:300])
        return started

    def run(self, job: dict, now: float | None = None, manual: bool = False) -> str:
        now = now or time.time()
        prompt = f"{job['prompt'].strip()}\n\n{STATUS_PROMPT}"
        stamp = time.strftime("%b %d %H:%M", time.localtime(now))
        s = self.create(prompt, project=job["project"], backend=job.get("backend") or "local",
                        model=job["model"] or None,
                        title=f"⏰ {job['name']} · {stamp}", job_id=job["id"])
        self.db.update_job(job["id"], last_run_at=now, last_session_id=s["id"], last_error="",
                           **({} if manual else {"next_run_at": Cron(job["cron"]).next_after(now)}))
        log.info("job %s started session %s%s", job["id"], s["id"], " (run now)" if manual else "")
        return s["id"]

    def _reschedule(self, job: dict, now: float, skipped: str = "", error: str = "") -> None:
        fields = {"next_run_at": Cron(job["cron"]).next_after(now)}
        if skipped:
            fields["last_skip"] = f"{time.strftime('%b %d %H:%M', time.localtime(now))}: {skipped}"
        if error:
            fields["last_error"] = error
        self.db.update_job(job["id"], **fields)


def new_job_id() -> str:
    return "j-" + uuid.uuid4().hex[:8]
