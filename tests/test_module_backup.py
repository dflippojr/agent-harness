"""Issue #334 stage (c): the nightly backup is an add-on module. Present it behaves as before (a manual backup, the
schedule, /maintenance, metrics); absent the daemon runs without the route, the settings and the schedule."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import cli
from harness.api import create_app
from harness.config import BackupConfig
from harness.llm import Completion
from harness.manager import Manager
from harness.metrics import render
from harness.settings_keys import build_registry
from harness_modules.backup import settings as backup_settings
from harness_modules.backup.runtime import doctor

from test_daemon import Script, make_cfg
from test_modules import route_paths

KEYS = ("backup.enabled", "backup.at", "backup.keep_days", "backup.dir", "backup.member_key_dir")


def make(tmp_path, packages=None, enabled=False) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=enabled, dir=str(tmp_path / "backups"))
    cfg.module_packages = packages
    return Manager(cfg, chat=Script([Completion(content="hi")]))


def test_backup_registers_through_the_interface(tmp_path):
    m = make(tmp_path)
    runtime = m.modules.get("backup")
    assert runtime is not None and runtime.service is m.backup
    assert "/maintenance/backup" in route_paths(create_app(m))
    specs = build_registry(m.cfg).specs
    assert all(key in specs for key in KEYS)
    assert any(row[0] == "maintenance backup" for row in cli.admin_commands())
    assert runtime.module.doctor is not None  # `python -m harness.doctor` runs it through Module.doctor


def test_a_manual_backup_works_while_the_schedule_is_off(tmp_path):
    m = make(tmp_path, enabled=False)
    with TestClient(create_app(m)) as client:
        done = client.post("/maintenance/backup").json()
        assert (tmp_path / "backups").is_dir() and done["ok_at"]
        assert "harness_backup_last_success_timestamp_seconds" in render(m)
    assert m.modules.get("backup").service._backup_task is None


def test_usage_reports_the_backup_status(tmp_path):
    m = make(tmp_path)
    asyncio.run(m.backup.backup())
    status = m.maintenance.status_extras["backup"]()
    assert status["dir"] == str(tmp_path / "backups") and status["ok_at"] and status["enabled"] is False


def test_the_schedule_starts_with_the_daemon_and_follows_a_live_change(tmp_path):
    async def run():
        m = make(tmp_path, enabled=True)
        service = m.backup
        service.start()
        first = service._backup_task
        assert first is not None
        backup_settings.apply_backup_schedule(m, "03:30", "04:00")
        assert service._backup_task is not first
        await m.modules.stop()
        assert service._backup_task is None
    asyncio.run(run())


def test_apply_schedule_without_the_module_does_nothing():
    backup_settings.apply_backup_schedule(SimpleNamespace(modules=SimpleNamespace(get=lambda name: None)), 1, 2)


def test_settings_accessors_and_checks(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(dir=str(tmp_path / "x" / "backups"))
    assert backup_settings.check_backup(cfg) == ["backup.dir parent is missing"]
    cfg.backup.dir = " "
    assert backup_settings.check_backup(cfg) == ["backup.dir is not configured"]
    cfg.backup.dir = str(tmp_path / "ok")
    assert backup_settings.check_backup(cfg) == []
    backup_settings._set_enabled(cfg, 1)
    backup_settings._set_at(cfg, "04:15")
    backup_settings._set_keep(cfg, "7")
    assert (backup_settings._get_enabled(cfg), backup_settings._get_at(cfg), backup_settings._get_keep(cfg)) \
        == (True, "04:15", 7)
    with pytest.raises(ValueError):
        backup_settings._set_at(cfg, "25:00")
    assert backup_settings._dir_get(cfg) is None
    with pytest.raises(ValueError):
        backup_settings._dir_set(cfg, "x")


def test_doctor_reports_backup_status(monkeypatch):
    import httpx

    class Report:
        def __init__(self):
            self.lines = []
        ok = lambda self, name, detail: self.lines.append(("ok", name, detail))  # noqa: E731
        warn = lambda self, name, detail: self.lines.append(("warn", name, detail))  # noqa: E731

    def answer(body):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: SimpleNamespace(json=lambda: body))
        report = Report()
        doctor(report, SimpleNamespace(port=1))
        return report.lines

    assert answer({"backup": {"enabled": True, "ok_at": 1, "path": "p"}}) == [("ok", "Backups", "p")]
    assert answer({"backup": {"enabled": True}})[0][0] == "warn"
    assert answer({"backup": {"enabled": False}}) == []

    def down(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", down)
    report = Report()
    doctor(report, SimpleNamespace(port=1))
    assert report.lines == []


def test_absent_backup_runs_without_it(tmp_path):
    m = make(tmp_path, packages=["harness_modules.images"], enabled=True)
    assert "backup" not in m.modules and m.backup is None
    paths = route_paths(create_app(m))
    assert "/maintenance/backup" not in paths
    specs = build_registry(m.cfg).specs
    assert not any(key in specs for key in KEYS)
    assert "backup" not in m.maintenance.status_extras
    m.modules.start()  # nothing schedules a backup
    with TestClient(create_app(m)) as client:
        assert client.post("/maintenance/backup").status_code in (404, 405)
    assert "harness_backup_last_success_timestamp_seconds" not in render(m)
