"""#686: owner-only usage windows read metadata, including billing and calendar boundaries."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import time

import pytest

from harness import app_usage
from harness.admin import PREFIX
from harness.db import Database

from test_admin import bearer, make_client


ZERO = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def tally(requests, prompt, completion, cost):
    return {"requests": requests, "prompt_tokens": prompt, "completion_tokens": completion, "cost_usd": cost}


def record(db, app_id, when, prompt=1, completion=1, cost=0.0, billing="api_key", requests=1, backend="hosted"):
    db.write(lambda: db.conn.execute(
        "INSERT INTO usage (backend, session_id, app_id, prompt_tokens, completion_tokens, cost_usd, "
        "billing, requests, created_at) VALUES (?, 'private-session', ?, ?, ?, ?, ?, ?, ?)",
        (backend, app_id, prompt, completion, cost, billing, requests, when)))


def freeze(monkeypatch, now):
    monkeypatch.setattr(app_usage, "time", SimpleNamespace(time=lambda: now.timestamp()))


def test_usage_windows_billing_zero_days_and_metadata_only(tmp_path, monkeypatch):
    client, manager = make_client(tmp_path)
    now = datetime(2026, 10, 15, 12)
    freeze(monkeypatch, now)
    db = manager.db.main
    app, _ = db.create_api_key("Shop", "sessions", "app")
    idle, _ = db.create_api_key("Idle", "sessions", "app")
    device, _ = db.create_api_key("Phone", "inference", "device")
    owner, _ = db.create_api_key("Hub", "admin", "owner")
    first = datetime(2026, 9, 16)
    week = datetime(2026, 10, 9)
    record(db, app["id"], now.timestamp(), 10, 5, billing="local", requests=2)
    record(db, app["id"], now.timestamp(), 20, 6, .5)
    record(db, app["id"], now.timestamp(), 3, 1, .25, requests=3, backend="other")
    record(db, app["id"], week.timestamp(), 30, 8, .125, "subscription")
    record(db, app["id"], (week - timedelta(microseconds=100)).timestamp(), 40, 9, 1)
    record(db, app["id"], first.timestamp(), 50, 10, 2)
    # Both temporal ends are bounded, including future records on the same calendar day.
    for when in [first - timedelta(seconds=1), now + timedelta(seconds=1), now + timedelta(days=1)]:
        record(db, app["id"], when.timestamp(), 1000, 1000, 1000)
    record(db, device["id"], now.timestamp(), 7, 2, .125)
    for app_id in ["", "missing-app", owner["id"], "app-web"]:
        record(db, app_id, now.timestamp(), 999, 999, 999)
    with client:
        # This route must never inspect App session stores or session content.
        monkeypatch.setattr(manager.db, "for_app", lambda *a, **k: pytest.fail("opened an App store"))
        before = db.write(lambda: db.conn.total_changes)
        response = client.get(PREFIX + "/apps/usage")
        assert response.status_code == 200, response.text
        assert db.write(lambda: db.conn.total_changes) == before
        result = response.json()
        assert (result["days"], result["start_date"], result["end_date"]) == (30, "2026-09-16", "2026-10-15")
        rows = {row["app_id"]: row for row in result["apps"]}
        assert set(rows) == {app["id"], idle["id"], device["id"]}
        used = rows[app["id"]]
        assert (used["name"], used["kind"]) == ("Shop", "app")
        assert used["today"] == tally(6, 33, 12, .75)
        assert used["week"] == tally(7, 63, 20, .875)
        assert used["month"] == tally(9, 153, 39, 3.875)
        assert len(used["daily"]) == 30
        assert used["daily"][0] == {"date": "2026-09-16", **tally(1, 50, 10, 2)}
        assert used["daily"][1] == {"date": "2026-09-17", **ZERO}
        assert used["daily"][-1] == {"date": "2026-10-15", **used["today"]}
        assert used["by_billing"]["local"]["month"] == tally(2, 10, 5, 0)
        assert used["by_billing"]["api_key"]["today"] == tally(4, 23, 7, .75)
        assert used["by_billing"]["subscription"]["today"] == ZERO
        assert used["by_billing"]["subscription"]["week"] == tally(1, 30, 8, .125)
        for window in ["today", "week", "month"]:
            for field in ZERO:
                assert sum(part[window][field] for part in used["by_billing"].values()) == used[window][field]
        for i, day in enumerate(used["daily"]):
            for field in ZERO:
                assert sum(part["daily"][i][field] for part in used["by_billing"].values()) == day[field]
        assert rows[idle["id"]]["month"] == ZERO
        assert rows[idle["id"]]["by_billing"] == {}
        assert all(day == {"date": day["date"], **ZERO} for day in rows[idle["id"]]["daily"])
        assert rows[device["id"]]["month"] == tally(1, 7, 2, .125)
        assert "private-session" not in response.text
        short = client.get(PREFIX + "/apps/usage?days=1").json()
        short_app = next(row for row in short["apps"] if row["app_id"] == app["id"])
        assert short["start_date"] == short["end_date"] == "2026-10-15"
        assert short_app["month"] == used["month"]
        assert short_app["daily"] == [used["daily"][-1]]
        assert short_app["by_billing"]["local"]["daily"] == [used["by_billing"]["local"]["daily"][-1]]


def test_fractional_timestamps_do_not_round_into_the_next_day(tmp_path, monkeypatch):
    client, manager = make_client(tmp_path)
    now = datetime(2026, 10, 15, 12)
    freeze(monkeypatch, now)
    db = manager.db.main
    app, _ = db.create_api_key("Midnight", "sessions", "app")
    midnight = datetime(2026, 10, 15)
    week = midnight - timedelta(days=6)
    first = midnight - timedelta(days=29)
    record(db, app["id"], (midnight - timedelta(microseconds=100)).timestamp(), 11, 1, .125)
    record(db, app["id"], (week - timedelta(microseconds=100)).timestamp(), 22, 1, .25)
    record(db, app["id"], (midnight + timedelta(microseconds=100)).timestamp(), 33, 1, .5)
    record(db, app["id"], (first - timedelta(microseconds=100)).timestamp(), 999, 999, 999)
    with client:
        used = client.get(PREFIX + "/apps/usage").json()["apps"][0]
        assert used["today"] == tally(1, 33, 1, .5)
        assert used["week"] == tally(2, 44, 2, .625)
        assert used["month"] == tally(3, 66, 3, .875)
        daily = {day["date"]: day for day in used["daily"]}
        assert daily["2026-10-14"]["prompt_tokens"] == 11
        assert daily["2026-10-08"]["prompt_tokens"] == 22
        assert daily["2026-10-09"]["prompt_tokens"] == 0
        assert used["by_billing"]["api_key"]["today"] == used["today"]


def test_revoked_and_erased_app_usage_remains_visible(tmp_path, monkeypatch):
    client, manager = make_client(tmp_path)
    now = datetime(2026, 10, 15, 12)
    freeze(monkeypatch, now)
    db = manager.db.main
    app, _ = db.create_api_key("Former App", "sessions", "app")
    record(db, app["id"], now.timestamp(), 12, 3, .25)
    db.revoke_api_key(app["id"])
    with client:
        path = PREFIX + "/apps/usage"
        revoked = client.get(path).json()["apps"]
        assert revoked[0]["month"] == tally(1, 12, 3, .25)
        db.write(lambda: db.conn.execute("UPDATE api_keys SET erased_at = ? WHERE id = ?",
                                        (now.timestamp(), app["id"])))
        assert client.get(path).json()["apps"] == revoked


def test_usage_auth_discovery_empty_and_validation(tmp_path):
    client, manager = make_client(tmp_path)
    db = manager.db.main
    with client:
        path = PREFIX + "/apps/usage"
        empty = client.get(path)
        assert empty.status_code == 200 and empty.json()["apps"] == []
        assert {"method": "GET", "path": path} in client.get(PREFIX).json()["operations"]
        schema = client.get("/openapi.json").json()
        assert schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/AppsUsageResponse"}
        for days in ["0", "31", "-1", "1.5", "invalid"]:
            assert client.get(path, params={"days": days}).status_code == 422
        for kind, scopes, status in [("owner", "admin", 200), ("owner", "sessions", 403),
                                      ("app", "admin", 403), ("device", "admin", 403)]:
            _, token = db.create_api_key(kind, scopes, kind)
            assert client.get(path, headers=bearer(token)).status_code == status
        assert client.get(path, headers=bearer("invalid")).status_code == 401
        assert client.get(path, headers={"Tailscale-User-Login": "stranger@example.com"}).status_code == 403
        assert client.get("/api/v1/apps/usage").status_code == 404
        assert client.get("/apps/usage").status_code == 404


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="process-local TZ switching requires POSIX tzset")
@pytest.mark.parametrize("now", [datetime(2026, 3, 15, 12), datetime(2026, 11, 15, 12)])
def test_local_calendar_boundaries_across_dst(tmp_path, monkeypatch, now):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        freeze(monkeypatch, now)
        db = Database(tmp_path / "usage.sqlite3")
        try:
            app, _ = db.create_api_key("DST", "sessions", "app")
            first = datetime.combine(now.date() - timedelta(days=29), datetime.min.time())
            record(db, app["id"], first.timestamp(), 10, 2)
            record(db, app["id"], (first - timedelta(seconds=1)).timestamp(), 999, 999)
            midnight = datetime.combine(now.date(), datetime.min.time())
            record(db, app["id"], (midnight - timedelta(seconds=1)).timestamp(), 20, 3)
            result = app_usage.snapshot(db)["apps"][0]
            assert result["month"] == tally(2, 30, 5, 0)
            assert result["today"] == ZERO
            assert result["daily"][0]["prompt_tokens"] == 10
            assert result["daily"][-2]["prompt_tokens"] == 20
        finally:
            db.close()
    finally:
        monkeypatch.undo()
        time.tzset()
