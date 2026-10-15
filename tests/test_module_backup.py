"""Issue #334 stage (c): the nightly backup is an add-on module: a manual backup, the schedule, /maintenance and
metrics. Whether it is present, off or absent is the lifecycle table in test_module_lifecycle.py."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness.api import create_app
from harness.config import BackupConfig
from harness.llm import Completion
from harness.manager import Manager
from harness.metrics import render
from harness_modules.backup import settings as backup_settings
from harness_modules.backup.runtime import doctor

from test_daemon import Script, make_cfg


def make(tmp_path, enabled=False) -> Manager:
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=enabled, dir=str(tmp_path / "backups"))
    return Manager(cfg, chat=Script([Completion(content="hi")]))


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


def test_checks_and_hidden_paths(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(dir=str(tmp_path / "x" / "backups"))
    assert backup_settings.check_backup(cfg) == ["backup.dir parent is missing"]
    cfg.backup.dir = " "
    assert backup_settings.check_backup(cfg) == ["backup.dir is not configured"]
    cfg.backup.dir = str(tmp_path / "ok")
    assert backup_settings.check_backup(cfg) == []
    with pytest.raises(ValueError):
        backup_settings._set_at(cfg, "25:00")
    specs = {spec.key: spec for spec in backup_settings.specs()}
    for key in ("backup.dir", "backup.member_key_dir"):
        spec = specs[key]
        assert spec.getter(cfg) is None
        with pytest.raises(ValueError):
            spec.setter(cfg, "x")


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


