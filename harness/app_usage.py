"""Read-only per-App calendar-day usage for the Owner API (#686)."""

from __future__ import annotations

from datetime import datetime, timedelta
import time

from pydantic import BaseModel


class UsageTally(BaseModel):
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class DailyUsage(UsageTally):
    date: str


class UsageWindows(BaseModel):
    today: UsageTally
    week: UsageTally
    month: UsageTally
    daily: list[DailyUsage]


class AppUsage(UsageWindows):
    app_id: str
    name: str
    kind: str
    by_billing: dict[str, UsageWindows]


class AppsUsageResponse(BaseModel):
    days: int
    start_date: str
    end_date: str
    apps: list[AppUsage]


_FIELDS = ("requests", "prompt_tokens", "completion_tokens", "cost_usd")


def _windows(dates: list[str]) -> dict:
    return {"today": UsageTally().model_dump(), "week": UsageTally().model_dump(),
            "month": UsageTally().model_dump(),
            "daily": [{"date": day, **UsageTally().model_dump()} for day in dates]}


def _add(windows: dict, row: dict, today: str, week_start: str, daily_index: dict[str, int]) -> None:
    targets = [windows["month"]]
    if row["day"] >= week_start:
        targets.append(windows["week"])
    if row["day"] == today:
        targets.append(windows["today"])
    if row["day"] in daily_index:
        targets.append(windows["daily"][daily_index[row["day"]]])
    for target in targets:
        for field in _FIELDS:
            target[field] += row[field]


def snapshot(db, days: int = 30) -> dict:
    """Fixed calendar windows through now; days controls only the zero-filled daily series (1..30)."""
    now = time.time()
    today = datetime.fromtimestamp(now).date()
    month_start = today - timedelta(days=29)
    # Convert the local calendar boundary, rather than subtracting 30*86400 seconds across DST.
    since = datetime.combine(month_start, datetime.min.time()).timestamp()
    dates = [(today - timedelta(days=i)).isoformat() for i in reversed(range(days))]
    daily_index = {day: i for i, day in enumerate(dates)}
    week_start = (today - timedelta(days=6)).isoformat()
    end_date = today.isoformat()
    apps = {}
    for row in db.app_usage_daily(since, now):
        app_id = row["app_id"]
        if app_id not in apps:
            apps[app_id] = {"app_id": app_id, "name": row["name"], "kind": row["kind"],
                            **_windows(dates), "by_billing": {}}
        if row["day"] is None:
            continue
        app = apps[app_id]
        _add(app, row, end_date, week_start, daily_index)
        if row["billing"] not in app["by_billing"]:
            app["by_billing"][row["billing"]] = _windows(dates)
        _add(app["by_billing"][row["billing"]], row, end_date, week_start, daily_index)
    return {"days": days, "start_date": dates[0], "end_date": end_date, "apps": list(apps.values())}
