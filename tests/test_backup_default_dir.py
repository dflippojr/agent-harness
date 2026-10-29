"""Issue #837: the default backup.dir is platform-appropriate. Off Windows it is the XDG data dir, so a test run never
writes a `D:` folder into the checkout. Synthetic paths only; anything created is under tmp_path."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness import config
from harness.api import create_app
from harness.config import BackupConfig
from harness.llm import Completion
from harness.manager import Manager

from test_daemon import Script, make_cfg

WINDOWS_DEFAULT = "D:/My Backups/agent-harness"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A synthetic home and no XDG_DATA_HOME: the non-Windows default is ~/.local/share/agent-harness/backups."""
    synthetic = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: synthetic)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return synthetic


def test_the_default_is_absolute_and_not_under_the_working_directory(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    default = Path(BackupConfig().dir)
    assert default.is_absolute()
    assert not default.is_relative_to(Path.cwd())


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_default_is_the_xdg_data_dir_honouring_xdg_data_home(home, tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    xdg = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert BackupConfig().dir == str(xdg / "agent-harness" / "backups")


def test_the_default_falls_back_to_local_share_without_xdg_data_home(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert BackupConfig().dir == str(home / ".local" / "share" / "agent-harness" / "backups")


@pytest.mark.parametrize("value", ["relative/xdg", ""])
def test_a_relative_or_empty_xdg_data_home_is_ignored(home, monkeypatch, value):
    # The XDG Base Directory spec treats a relative or empty variable as unset.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert BackupConfig().dir == str(home / ".local" / "share" / "agent-harness" / "backups")


def test_windows_keeps_its_d_drive_default(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert BackupConfig().dir == WINDOWS_DEFAULT


def test_a_configured_backup_dir_wins_over_the_default(tmp_path):
    cfg_dir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parent.parent / "config", cfg_dir)
    text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    (cfg_dir / "harness.yaml").write_text(text.replace("dir: D:/My Backups/agent-harness", "dir: '/synthetic/backups'"),
                                          encoding="utf-8")
    assert config.load(cfg_dir, data_dir=tmp_path / "data").backup.dir == "/synthetic/backups"


def test_a_manual_backup_never_writes_a_d_folder_into_the_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    xdg = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    cfg = make_cfg(tmp_path)
    if sys.platform == "win32":  # the Windows default is the real D: drive, so keep this run's backups under tmp_path
        cfg.backup.dir = str(xdg / "agent-harness" / "backups")
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    with TestClient(create_app(m)) as client:
        assert client.post("/maintenance/backup").json()["ok_at"]
    assert "D:" not in os.listdir(checkout)
    assert (xdg / "agent-harness" / "backups").is_dir()
