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

Daylight-saving changes follow vixie-cron (#932, owner decision): a fixed time in the hour that repeats when clocks
fall back runs once, at the first pass (or the second, for a job created then); a schedule with `*` in the minute or
hour field runs on real time through both passes. See `Cron.next_after`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from harness.modules import operation_audit, audit_context
from harness.modules import parse_job_status as parse_status, job_summary as summary  # noqa: F401 -- tests import them from here

from .store import JobStore

log = logging.getLogger("harness.jobs")

NOTIFY_MODES = ("attention", "low", "always")  # OK results: no notification / low priority / normal priority
STATUS_PROMPT = ("This is a scheduled job, so the user isn't watching, and an OK result is barely shown to them. Do the "
                 "check, then end your final answer with one last line: `STATUS: OK` only if everything the task "
                 "expects is true, or `STATUS: ATTENTION: <one-line reason>` if anything isn't, even when there may be "
                 "a harmless explanation (say what it might be; the user decides). Don't make changes that need "
                 "approval unless the task says to.")
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
MONTH_NAMES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                           "nov", "dec"], 1)}
CATCH_UP_MAX_MINUTES = 525600  # one year (owner decision on #934)
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
    if not part:  # `0,30,` or `1,,15`: a typo, not every value
        raise CronError(f"empty list item in {text!r}")
    if "/" in part:
        part, step_text = part.split("/", 1)
        if not step_text.isdigit() or int(step_text) < 1:
            raise CronError(f"bad step in {text!r}")
        step = int(step_text)
    if part in ("*", ""):  # "" only from `/5`: an empty base before a step is a wildcard
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


class LocalClock:
    """The tower's local time zone (the process's TZ): the zone job schedules are written in."""

    def local(self, t: float) -> datetime:
        """Naive local wall-clock time of t; `fold=1` in the second pass of a repeated hour."""
        return datetime.fromtimestamp(t)

    def instant(self, d: datetime) -> float:
        """Timestamp of naive local wall-clock time d, honouring `fold` (PEP 495)."""
        return d.timestamp()

    def offset(self, t: float) -> float:
        return time.localtime(t).tm_gmtoff


class ZoneClock(LocalClock):
    """A named zone instead of the process's TZ (tests pin one this way on every OS)."""

    def __init__(self, zone):
        self.zone = zone

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.zone).replace(tzinfo=None)  # keeps fold

    def instant(self, d: datetime) -> float:
        return d.replace(tzinfo=self.zone).timestamp()

    def offset(self, t: float) -> float:
        offset = datetime.fromtimestamp(t, self.zone).utcoffset()
        return offset.total_seconds() if offset else 0.0


LOCAL = LocalClock()  # read at call time, so a test can swap in a ZoneClock


def _fall_backs(start: float, end: float):
    """(instant, seconds repeated) for each time between start and end that local clocks go back."""
    t = start
    while t < end:
        step = min(3600.0, end - t)
        drop = LOCAL.offset(t) - LOCAL.offset(t + step)
        if drop > 0:
            lo, hi = t, t + step  # the offset changes in (lo, hi]: narrow it to the minute
            while hi - lo > 60:
                mid = lo + (hi - lo) // 120 * 60
                lo, hi = (mid, hi) if LOCAL.offset(mid) == LOCAL.offset(lo) else (lo, mid)
            yield int(hi) // 60 * 60, drop
        t += step


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
        # vixie-cron: a `*` minute or hour field keeps running through a repeated hour; fixed times run once.
        self.wildcard = parts[0].startswith("*") or parts[1].startswith("*")

    def _day_ok(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.weekday() + 1) % 7 in self.weekdays
        if self.day_any:
            return dom or dow
        return dom and dow

    def _matches(self, d: datetime) -> bool:
        return d.month in self.months and self._day_ok(d) and d.hour in self.hours and d.minute in self.minutes

    def _next_wall(self, d: datetime) -> datetime:
        """The first matching naive local wall-clock minute at or after d (d has no seconds)."""
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
            return d
        raise CronError(f"{self.expr!r} never matches")

    def next_after(self, t: float) -> float:
        """The first matching minute strictly after timestamp t (local time).

        Local wall-clock times repeat when clocks fall back and skip when they spring forward (#932). A wall-clock
        minute that happens twice runs once (vixie-cron): at its first occurrence, or at its second when t is already
        in the second pass (a job created or re-planned then). Jobs with `*` in the minute or hour field (`*/5`,
        `0 * * * *`) keep their real-time rhythm: they run in both passes and not in skipped minutes. A fixed time the
        clocks skip runs at the same offset past the jump (02:30 -> 03:30), as before.
        """
        # Naive datetimes lose `fold` on any arithmetic, so walk the wall clock and pick the occurrence explicitly.
        d = LOCAL.local(t).replace(second=0, microsecond=0, fold=0) + timedelta(minutes=1)
        while True:
            d = self._next_wall(d)
            first, second = LOCAL.instant(d), LOCAL.instant(d.replace(fold=1))  # equal unless repeated or skipped
            found = None
            if second > first:  # repeated: the first occurrence, or the second if t is past the first
                if first > t:
                    found = first
                elif second > t:
                    found = second
            elif (second == first or not self.wildcard) and first > t:
                found = first  # ordinary; a skipped fixed time runs that far past the jump (wildcards skip it)
            if found is not None:
                break
            d += timedelta(minutes=1)
        if self.wildcard:
            found = self._second_pass_before(t, found)
        return found

    def _second_pass_before(self, t: float, found: float) -> float:
        """A wildcard job's matching minute in a repeated hour between t and found. The wall-clock walk from t's
        first-pass time jumps over the second pass, so each fall-back between them is checked minute by minute."""
        for change, repeat in _fall_backs(t, found):
            m = change
            while m < min(change + repeat, found):
                d = LOCAL.local(m)
                if m > t and d.fold and self._matches(d):
                    return float(m)
                m += 60
        return found

    def next_slot(self, now: float, since: float | None) -> float:
        """next_after(now) for a job created at `since` (its created_at; None: long ago). Every re-plan goes through
        here. A fixed-time job takes the second occurrence of a repeated wall-clock minute only if it was created
        during that second pass, at or after the instant the clocks went back (vixie-cron, owner decision on #932). A
        job that existed before it had the first pass: it ran then, or the slot was skipped."""
        nxt = self.next_after(now)
        while not self.wildcard and self._second_pass_barred(nxt, since):
            nxt = self.next_after(nxt)
        return nxt

    @staticmethod
    def _second_pass_barred(t: float, since: float | None) -> bool:
        d = LOCAL.local(t)
        if not d.fold:
            return False
        first = LOCAL.instant(d.replace(fold=0))
        if first >= t:
            return False  # not a second occurrence
        went_back = next((change for change, _ in _fall_backs(first, t)), None)
        return since is None or went_back is None or since < went_back

    def describe(self) -> str:
        return self.expr


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
    cron.next_after(time.time())  # CronError when it parses but never matches (`0 0 30 2 *`, #934)
    project = job.get("project") or "scratch"
    if project not in projects:
        raise ValueError(f"unknown project {project!r}")
    model = job.get("model") or ""
    backend = job.get("backend") or "local"
    _check_backend(backend, model, models, backends)
    notify = job.get("notify") or "low"
    if notify not in NOTIFY_MODES:
        raise ValueError(f"notify must be one of {', '.join(NOTIFY_MODES)}")
    catch_up = int(job.get("catch_up_minutes", 360))
    if catch_up > CATCH_UP_MAX_MINUTES:
        raise ValueError(f"catch_up_minutes must be at most {CATCH_UP_MAX_MINUTES} (one year)")
    return {"name": name[:80], "prompt": prompt, "cron": cron.expr, "project": project, "backend": backend,
            "model": model,
            "notify": notify, "enabled": bool(job.get("enabled", True)),
            "catch_up_minutes": max(0, catch_up)}


class JobScheduler:
    """Starts sessions for due jobs. `create` is Manager.create; `active` tells whether a session is still running."""

    def __init__(self, db, create, active, poll_seconds: float = 30):
        self.db = db
        self.store = JobStore(db)
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

    def _schedule_ok(self, job: dict) -> bool:
        """A job saved before its schedule became invalid (#933: `0,30,`) is parked, not run: no next run, an error."""
        try:
            Cron(job["cron"])
            return True
        except CronError as e:
            log.warning("job %s has an invalid schedule %r: %s", job["id"], job["cron"], e)
            self.store.update_job(job["id"], next_run_at=None, last_error=f"invalid schedule, edit the job: {e}"[:300])
            return False

    def _catch_up(self, now: float) -> None:
        """At start: jobs whose slot passed while the daemon was down run once if the miss is recent, else skip."""
        for job in self.store.list_jobs():
            if not job["enabled"] or not job["next_run_at"] or job["next_run_at"] > now:
                continue
            if not self._schedule_ok(job):
                continue
            if now - job["next_run_at"] <= job["catch_up_minutes"] * 60:
                continue  # tick() runs it
            self._reschedule(job, now, skipped="missed while the tower or daemon was down")

    def tick(self, now: float) -> list[str]:
        started = []
        for job in self.store.list_jobs():
            if not job["enabled"] or not job["next_run_at"] or job["next_run_at"] > now:
                continue
            if not self._schedule_ok(job):
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

    def run(self, job: dict, now: float | None = None, manual: bool = False, *, context=None) -> str:
        now = now or time.time()
        prompt = f"{job['prompt'].strip()}\n\n{STATUS_PROMPT}"
        stamp = time.strftime("%b %d %H:%M", time.localtime(now))
        context = context if manual else audit_context.AuditContext("system", "system", "", "job")
        with operation_audit.operation(self.db, context, job["id"], "job.run",
                                       {"trigger": "manual" if manual else "scheduled"},
                                       scheduled=not manual) as audit:
            s = self.create(prompt, project=job["project"], backend=job.get("backend") or "local",
                            model=job["model"] or None,
                            title=f"⏰ {job['name']} · {stamp}", job_id=job["id"])
            planned = {} if manual else {"next_run_at": Cron(job["cron"]).next_slot(now, job.get("created_at"))}
            self.store.update_job(job["id"], last_run_at=now, last_session_id=s["id"], last_error="", **planned)
            log.info("job %s started session %s%s", job["id"], s["id"], " (run now)" if manual else "")
            audit["resulting_session_id"] = s["id"]
        return s["id"]

    def _reschedule(self, job: dict, now: float, skipped: str = "", error: str = "") -> None:
        fields = {"next_run_at": Cron(job["cron"]).next_slot(now, job.get("created_at"))}
        if skipped:
            fields["last_skip"] = f"{time.strftime('%b %d %H:%M', time.localtime(now))}: {skipped}"
        if error:
            fields["last_error"] = error
        self.store.update_job(job["id"], **fields)


def new_job_id() -> str:
    return "j-" + uuid.uuid4().hex[:8]
