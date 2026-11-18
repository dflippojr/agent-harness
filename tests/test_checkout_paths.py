"""Stage (e) of #334: nothing in the core resolves a required file relative to a checkout when it is installed.

`harness.config.CHECKOUT` is the only door to repository files: the checkout's config/, data/, stables/ and bakeoff/
when the Server runs from one, None when it is installed from the wheel (site-packages holds the package, and none of
the repository). Installed, the defaults are the per-user install the platform installers write. Add-on modules
read repository files through `harness.modules.repo_file`, which prefers the copy their wheel carries.
tests/test_packaging.py runs the real wheel; these tests pin each default and the rule itself.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from harness import canary, config, modules, stables_shell
from harness.config import BackupConfig
from harness.llm import Completion
from harness.manager import Manager
from harness_modules.hub import entries as hub_entries
from harness_modules.runners import mac_client

from test_daemon import Script, make_cfg

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """The core as the wheel installs it: no checkout, a synthetic home, and no variable naming a directory."""
    monkeypatch.setattr(config, "CHECKOUT", None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for name in ("XDG_DATA_HOME", "HARNESS_CONFIG_DIR", "HARNESS_DATA_DIR", "HARNESS_BUILD_COMMIT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home" / ".local" / "share" / "agent-harness"


# ----------------------------------------------------------------------------------------- the checkout marker

def test_this_repository_is_a_checkout_and_site_packages_is_not(tmp_path):
    assert config.CHECKOUT == ROOT
    site = tmp_path / "site-packages"
    (site / "harness").mkdir(parents=True)
    (site / "harness" / "config.py").write_text("", encoding="utf-8")
    (site / "config").mkdir()  # some other distribution's top-level folder
    assert config._source_checkout(site) is None
    (site / "pyproject.toml").write_text("", encoding="utf-8")  # one stray file is not a checkout either
    assert config._source_checkout(site) is None
    (site / "config" / "harness.yaml").write_text("", encoding="utf-8")
    assert config._source_checkout(site) == site


# ------------------------------------------------------------------------------------------------- the defaults

def test_a_checkout_keeps_its_own_config_and_data_dirs(monkeypatch):
    monkeypatch.delenv("HARNESS_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert config.resolve_config_dir() == ROOT / "config"
    assert config._default_data_dir() == ROOT / "data"
    assert config._default_repos_dir() == ROOT / "data" / "repos"
    assert stables_shell.DEFAULT == ROOT / "stables" / "public"


def test_installed_the_defaults_are_the_per_user_install(installed, tmp_path):
    assert config.install_home() == installed
    assert config.resolve_config_dir() == installed / "config"
    assert config._default_data_dir() == installed / "data"
    assert config._default_repos_dir() == installed / "data" / "repos"
    assert config.Config(host="", port=0, data_dir=tmp_path, repos_dir=tmp_path, default_model="", models={},
                         sandbox=config.SandboxConfig(), projects={}).config_dir == installed / "config"
    assert config._load_canary({}).fixture_dir == str(installed / "data" / "web-fixture")


def test_installed_on_windows_the_defaults_follow_install_ps1(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert config.install_home() == local / "agent-harness"
    assert config._default_data_dir() == local / "agent-harness" / "data"
    monkeypatch.setenv("LOCALAPPDATA", "relative")  # unusable, like a relative XDG_DATA_HOME
    assert config.install_home() == tmp_path / "home" / "AppData" / "Local" / "agent-harness"


def test_installed_the_xdg_data_home_moves_the_install(installed, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.resolve_config_dir() == tmp_path / "xdg" / "agent-harness" / "config"


@pytest.mark.parametrize("where", ["argument", "environment"])
def test_a_named_config_dir_wins_over_every_default(installed, tmp_path, monkeypatch, where):
    named = tmp_path / "named"
    named.mkdir()
    (named / "harness.yaml").write_text(yaml.safe_dump({"listen": {"port": 8123}, "data_dir": str(tmp_path / "d")}),
                                        encoding="utf-8")
    if where == "environment":
        monkeypatch.setenv("HARNESS_CONFIG_DIR", str(named))
    argument = named if where == "argument" else None
    assert config.resolve_config_dir(argument) == named
    assert config.resolve_port(argument) == 8123
    assert config.resolve_data_dir(argument) == tmp_path / "d"


def test_installed_the_default_config_is_read_from_the_per_user_install(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_apply_managed_overlay", lambda cfg: None)
    (installed / "config").mkdir(parents=True)
    raw = {"listen": {"port": 8124}, "models": {"m": {"base_url": "http://127.0.0.1:1", "context_tokens": 1024}}}
    (installed / "config" / "harness.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = config.load()
    assert (cfg.config_dir, cfg.port, cfg.data_dir) == (installed / "config", 8124, installed / "data")


def test_an_installed_cli_reaches_the_installed_daemons_port(installed):
    """The review of #1001: the installed `harness` command read the install's token but assumed port 8100."""
    from harness import cli
    assert cli._local_daemon_url() == "http://127.0.0.1:8100"  # no install configured yet: the default
    (installed / "config").mkdir(parents=True)
    (installed / "config" / "harness.yaml").write_text("listen: {port: 8126}\n", encoding="utf-8")
    assert cli._local_daemon_url() == "http://127.0.0.1:8126"


def test_a_checkouts_cli_keeps_the_default_port_unless_an_install_is_named(tmp_path, monkeypatch):
    from harness import cli
    monkeypatch.delenv("HARNESS_CONFIG_DIR", raising=False)
    assert cli._local_daemon_url() == "http://127.0.0.1:8100"
    named = tmp_path / "named"
    named.mkdir()
    (named / "harness.yaml").write_text("listen: {port: 8127}\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(named))
    assert cli._local_daemon_url() == "http://127.0.0.1:8127"


def test_installed_stables_is_served_only_from_the_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(stables_shell, "DEFAULT", None)  # what an installed core computes: no checkout
    monkeypatch.delenv(stables_shell.ENV, raising=False)
    assert stables_shell.locate() is None
    shell = tmp_path / "shell"
    shell.mkdir()
    (shell / "index.html").write_text("stand-in", encoding="utf-8")
    monkeypatch.setenv(stables_shell.ENV, str(shell))
    assert stables_shell.locate() == shell.resolve()


# ------------------------------------------------------------------------------------------------------ canary

def test_the_canary_suite_resolves_only_inside_a_checkout(installed, tmp_path):
    suite = tmp_path / "suite.yaml"
    assert config.canary_suite_path(str(suite)) == suite
    assert config.canary_suite_path("bakeoff/canary.yaml") is None
    assert config._canary_fixture_problem({"enabled": True, "suite": "bakeoff/canary.yaml"}) == (
        "canary.suite 'bakeoff/canary.yaml' is not a file")


def test_from_a_checkout_the_canary_suite_is_the_checkouts():
    assert config.canary_suite_path("bakeoff/canary.yaml") == ROOT / "bakeoff" / "canary.yaml"
    assert config.canary_runner_problem() == ""


def test_without_bakeoff_the_canary_is_off_and_the_daemon_starts(tmp_path, monkeypatch, caplog):
    """The review of 2026-11-17: the core canary imports the dev-only bakeoff package, which a wheel leaves out."""
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "bakeoff" else real(name, *a))
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with caplog.at_level("ERROR", logger="harness.config"):
        cfg = config._load_canary({"enabled": True, "fixture_dir": str(tmp_path)})
    assert cfg.enabled is False and "bakeoff" in cfg.disabled_reason
    assert any("bakeoff" in r.getMessage() for r in caplog.records)


def test_a_canary_whose_runner_cannot_load_skips_the_commit(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="hi")]))
        try:
            monkeypatch.setitem(sys.modules, "bakeoff.canary", None)  # import fails, as without the package
            with pytest.raises(canary.CanaryConfigError, match="did not load"):
                await m._build_canary().canary.run_suite("a" * 40, None)
        finally:
            m.db.close()
    asyncio.run(body())


def test_installed_the_deployed_commit_comes_only_from_the_deployer(installed, monkeypatch):
    def no_git(*args, **kwargs):
        raise AssertionError("git must not answer for the repository site-packages sits in")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert canary.deployed_sha() == ""
    monkeypatch.setenv("HARNESS_BUILD_COMMIT", "b" * 40)
    assert canary.deployed_sha() == "b" * 40


# ------------------------------------------------------------------------------------------ add-on modules' files

def test_repo_file_prefers_the_copy_an_add_on_wheel_carries(tmp_path, monkeypatch):
    package = tmp_path / "harness_modules" / "probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "harness_modules.probe", raising=False)
    import importlib
    importlib.import_module("harness_modules.probe")
    try:
        assert modules.repo_file("harness_modules.probe", "docs/modules.md") == ROOT / "docs/modules.md"
        carried = package / modules.REPO_FILES / "docs/modules.md"
        monkeypatch.setattr(config, "CHECKOUT", None)
        assert modules.repo_file("harness_modules.probe", "docs/modules.md") == carried  # missing: named on read
        carried.parent.mkdir(parents=True)
        carried.write_text("carried", encoding="utf-8")
        monkeypatch.setattr(config, "CHECKOUT", ROOT)
        assert modules.repo_file("harness_modules.probe", "docs/modules.md") == carried
    finally:
        sys.modules.pop("harness_modules.probe", None)


def test_from_a_checkout_modules_read_the_checkouts_files():
    assert hub_entries.SCHEMA == ROOT / "docs/marketplace-manifest.schema.json"
    assert hub_entries.EXAMPLE == ROOT / "docs/hub.entries.example.json"
    assert mac_client.source("macrunner/install.sh") == ROOT / "macrunner" / "install.sh"
    assert mac_client.source("sdk/harness_client.py") == ROOT / "sdk" / "harness_client.py"
    assert mac_client.source("harness/cli.py") == ROOT / "harness" / "cli.py"  # the running core's own file


def test_the_backup_holds_the_config_the_daemon_runs_with(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.backup = BackupConfig(enabled=False, dir=str(tmp_path / "backups"))
    cfg.config_dir = tmp_path / "live-config"
    cfg.config_dir.mkdir()
    (cfg.config_dir / "harness.yaml").write_text("listen: {port: 8125}\n", encoding="utf-8")
    m = Manager(cfg, chat=Script([Completion(content="hi")]))
    try:
        result = asyncio.run(m.backup.backup())
    finally:
        m.db.close()
    copied = Path(result["path"]) / "config"
    assert (copied / "harness.yaml").read_text(encoding="utf-8") == "listen: {port: 8125}\n"
    assert not (copied / "projects.yaml").exists()  # only what the running config has, never the checkout's


def test_hub_entries_default_to_the_resolved_config_dir(installed, monkeypatch):
    import argparse
    seen = []
    monkeypatch.setattr(hub_entries, "initialize", lambda config_dir: seen.append(config_dir) or config_dir)
    assert hub_entries._command(argparse.Namespace(config_dir=None, entries_action="init")) == 0
    assert seen == [installed / "config"]


# -------------------------------------------------------------------------------------------- the rule, as a lint

# How the core may build a path from its own location: package data next to the file (Path(__file__).parent / ...)
# is fine, the folder above the package is not. These are the only lines that name it, each for a stated reason.
CHECKOUT_RELATIVE = re.compile(r"\bROOT\b|__file__\)(?:\.resolve\(\))?\.parent\.parent|\.parents\[")
ALLOWED = {
    # ROOT's definition, and the checkout test: every default that names a repository file goes through CHECKOUT.
    "harness/config.py": ["ROOT = Path(__file__).resolve().parent.parent", "CHECKOUT = _source_checkout(ROOT)"],
    # A refusal, not a lookup: an OAuth client secret must not sit inside the code's own tree, checkout or install.
    "harness/google_signin.py": ["from .config import ROOT", "if _inside(path, ROOT):"],
}


def test_the_core_and_modules_name_the_checkout_only_through_config_checkout():
    """The class of bug the 2026-11-17 review found: a default resolved against the folder above the package, which
    is the checkout from a clone and site-packages once installed. New code uses config.CHECKOUT (explicitly
    checkout-only, None when installed), package data, or harness.modules.repo_file for an add-on."""
    found: dict[str, list[str]] = {}
    for tree in ("harness", "harness_modules"):
        for path in sorted((ROOT / tree).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                     if CHECKOUT_RELATIVE.search(line) and not line.strip().startswith("#")]
            if lines:
                found[rel] = lines
    assert found == ALLOWED
