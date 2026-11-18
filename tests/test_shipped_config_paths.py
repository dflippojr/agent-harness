"""Issue #854: config/harness.yaml names no Windows path, so nothing loaded from it can write a relative `D:` folder off
Windows. The values moved to platform defaults in harness/config.py: a Windows host resolves what the file used to say,
and harness.local.yaml still wins everywhere. Synthetic paths only; anything created is under tmp_path."""

from __future__ import annotations

import dataclasses
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from harness import config, setup_config
from harness.config import BackendConfig, HomelabConfig, ImagesConfig

ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[/\\]")

# What config/harness.yaml and the dataclasses said before #854. A Windows host must keep every one of these.
WINDOWS_VALUES = {
    "notify.token_file": "D:/Docker/ntfy/secrets/harness-publisher.token",
    "backends.claude.api_key_file": "D:/Agents/harness/secrets/claude-api-key",
    "backends.codex.api_key_file": "D:/Agents/harness/secrets/codex-api-key",
    "backends.cursor.api_key_file": "D:/Agents/harness/secrets/cursor-api-key",
    "homelab.docker_root": "D:/Docker",
    "runners.macbook.token_file": "D:/Agents/harness/secrets/runner-macbook.token",
    "gpu_guard.pause_flag": "C:/AI/llama-server.paused",
    "backup.dir": "D:/My Backups/agent-harness",
    "memory_library.clone_dir": "D:/Agents/memory-library",
    "images.comfy_dir": "C:/AI/ComfyUI",
    "images.models_dir": "C:/AI/comfy-models",
    "images.work_dir": "D:/Agents/harness/images-work",
    "images.log_dir": "D:/Agents/harness/logs",
}
WINDOWS_FOLDERS = {"agent-harness": "D:/Projects/agent-harness",
                   "agent-memory-library": "~/OneDrive/Documents/agent-memory-library"}


def _shipped_config(tmp_path) -> Path:
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    return cfg_dir


def _strings(value, where=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}" if where else str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, where)
    elif isinstance(value, (str, Path)):
        yield where, str(value)


def _lookup(cfg, dotted: str):
    value = cfg
    for part in dotted.split("."):
        value = value[part] if isinstance(value, dict) else getattr(value, part)
    return value


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A synthetic home with no XDG_DATA_HOME or data-dir override, and a working directory that is not the checkout."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for name in ("XDG_DATA_HOME", "HARNESS_DATA_DIR", "HARNESS_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(config, "_apply_managed_overlay", lambda cfg: None)  # takes a real Windows file lock under "win32"
    return tmp_path


def test_the_shipped_file_names_no_windows_path():
    raw = yaml.safe_load((ROOT / "config" / "harness.yaml").read_text(encoding="utf-8"))
    assert [f"{where}: {text}" for where, text in _strings(raw) if WINDOWS_PATH.search(text)] == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_shipped_config_resolves_no_windows_path_off_windows(machine, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    cfg_dir = _shipped_config(machine)
    resolved = dataclasses.asdict(config.load(cfg_dir, data_dir=machine / "data"))
    resolved.pop("projects")  # config/projects.yaml is sample data, outside this issue
    resolved["resolved_data_dir"] = config.resolve_data_dir(cfg_dir)
    # A Windows CI checkout or temp dir has a drive letter of its own, which says nothing about the config.
    leftovers = [f"{where}: {text}" for where, text in _strings(resolved)
                 if WINDOWS_PATH.search(text.replace(str(ROOT), "<root>").replace(str(machine), "<tmp>"))]
    assert leftovers == []


def test_the_defaults_off_windows_are_absolute(machine, monkeypatch):
    # A relative default is the bug itself: it lands in whatever the working directory is.
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = config.load(_shipped_config(machine), data_dir=machine / "data")
    for dotted in ("homelab.docker_root", "gpu_guard.pause_flag", "backup.dir", "memory_library.clone_dir",
                   "images.comfy_dir", "images.models_dir", "images.work_dir", "images.log_dir"):
        assert Path(_lookup(cfg, dotted)).is_absolute(), dotted
    assert config.resolve_data_dir(_shipped_config(machine / "again")).is_absolute()
    assert cfg.repos_dir.is_absolute()


def test_off_windows_an_unset_data_dir_is_this_checkouts_not_the_deployed_daemons(monkeypatch):
    # The XDG data dir is where install.sh puts a deployed daemon's database: a checkout's run must not open it.
    monkeypatch.setattr(sys, "platform", "linux")
    assert config._default_data_dir() == ROOT / "data"
    assert config._default_repos_dir() == ROOT / "data" / "repos"


def test_the_xdg_defaults_honour_xdg_data_home(machine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(machine / "xdg"))
    home = machine / "xdg" / "agent-harness"
    assert ImagesConfig().work_dir == str(home / "images-work")
    assert HomelabConfig().docker_root == str(home / "docker")


def test_key_files_have_no_default_off_windows(machine, monkeypatch):
    # Nothing to find means no key, not a guess at where one might be.
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = config.load(_shipped_config(machine), data_dir=machine / "data")
    assert BackendConfig().api_key_file == ""
    assert {cfg.backends[name].api_key_file for name in ("claude", "codex", "cursor")} == {""}
    assert cfg.notify.token_file == ""
    assert cfg.runners["macbook"].token_file == ""
    assert cfg.remote_control.folders == {}


def test_the_shipped_config_on_windows_resolves_what_the_file_used_to_say(machine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg_dir = _shipped_config(machine)
    cfg = config.load(cfg_dir, data_dir=machine / "data")
    assert {key: _lookup(cfg, key) for key in WINDOWS_VALUES} == WINDOWS_VALUES
    assert cfg.remote_control.folders == WINDOWS_FOLDERS
    assert config.resolve_data_dir(cfg_dir) == Path("D:/Agents/harness")
    assert cfg.repos_dir == Path("D:/Agents/repos")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_local_overlay_wins_over_every_platform_default(machine, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    cfg_dir = _shipped_config(machine)
    overlay = {
        "data_dir": "/synthetic/data",
        "repos_dir": "/synthetic/repos",
        "notify": {"token_file": "/synthetic/ntfy.token"},
        "backends": {"codex": {"api_key_file": ""}},
        "homelab": {"docker_root": "/synthetic/docker"},
        "runners": {"macbook": {"token_file": "/synthetic/runner.token"}},
        "gpu_guard": {"pause_flag": "/synthetic/paused"},
        "memory_library": {"clone_dir": "/synthetic/memory"},
        "remote_control": {"folders": {"mine": "/synthetic/folder"}},
        "images": {"comfy_dir": "/synthetic/comfy", "models_dir": "/synthetic/models", "work_dir": "/synthetic/work",
                   "log_dir": "/synthetic/logs"},
    }
    (cfg_dir / "harness.local.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    cfg = config.load(cfg_dir, data_dir=machine / "data")
    assert config.resolve_data_dir(cfg_dir) == Path("/synthetic/data")
    assert cfg.repos_dir == Path("/synthetic/repos")
    assert cfg.notify.token_file == "/synthetic/ntfy.token"
    assert cfg.backends["codex"].api_key_file == ""
    assert cfg.homelab.docker_root == "/synthetic/docker"
    assert cfg.runners["macbook"].token_file == "/synthetic/runner.token"
    assert cfg.gpu_guard.pause_flag == "/synthetic/paused"
    assert cfg.memory_library.clone_dir == "/synthetic/memory"
    assert cfg.remote_control.folders == {"mine": "/synthetic/folder"}
    assert (cfg.images.comfy_dir, cfg.images.models_dir, cfg.images.work_dir, cfg.images.log_dir) == (
        "/synthetic/comfy", "/synthetic/models", "/synthetic/work", "/synthetic/logs")


def test_a_generated_windows_install_gets_no_token_file_or_folders_it_never_had(machine, monkeypatch):
    # An installed config has no D:/Docker or D:/Projects to point at, and the file is kept across upgrades: a folder
    # named like one of its projects would take over that project in Remote Control.
    monkeypatch.setattr(sys, "platform", "win32")
    args = setup_config.argparse.Namespace(
        model=next(iter(setup_config.PRESETS)), data_dir="/synthetic/data", port=8100, public_url="", login="",
        profile="full", llama_url="http://127.0.0.1:8090", enable_module=[], gpu_guard=True, pause_flag="/synthetic/p",
        images_models_dir="")
    cfg_dir = machine / "generated"
    cfg_dir.mkdir()
    generated = setup_config.build(args)
    assert "windows_tower_layout" not in generated
    generated["backends"] = {"codex": {"enabled": True}}
    generated["runners"] = {"macbook": {}}
    (cfg_dir / "harness.yaml").write_text(yaml.safe_dump(generated), encoding="utf-8")
    cfg = config.load(cfg_dir, data_dir=machine / "data")
    assert cfg.notify.token_file == ""
    assert cfg.remote_control.folders == {}
    assert cfg.runners["macbook"].token_file == ""
    assert cfg.backends["codex"].api_key_file == WINDOWS_VALUES["backends.claude.api_key_file"]  # as it always was


@pytest.mark.parametrize(("platform", "expected"), [("win32", "C:/AI/comfy-models"), ("linux", None)])
def test_an_empty_models_dir_falls_back_to_the_platform_default(machine, monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    resolved = config.resolve_images_models_dir(ImagesConfig(models_dir=""))
    if expected:
        assert resolved == Path(expected)
    else:
        assert resolved == machine / "home" / ".local" / "share" / "agent-harness" / "comfy-models"
