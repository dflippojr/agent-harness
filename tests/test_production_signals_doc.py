"""The sweep block in docs/production-signals.md (#894) reads the files the daemon writes on its Unix host.

The sweep lives in dflippojr/agent-ops (`scripts/signal_sweep.py`) and isn't importable here, so `_FENCE`, `_expand`
and the read-only URI below repeat what it does with this block.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import yaml

from harness.config import Config
from harness.db import Database

ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs" / "production-signals.md"
_FENCE = re.compile(r"^```ya?ml production-signals[ \t]*\r?\n(.*?)^```", re.M | re.S)
_DB = "~/.local/share/agent-harness/data/harness.sqlite3"
_LOG = "~/.local/share/agent-harness/logs/daemon.log"


def _block() -> str:
    match = _FENCE.search(DOC.read_text(encoding="utf-8"))
    assert match, f"{DOC.name} has no production-signals block, so the sweep falls back to the agent-ops seed"
    return match.group(1)


def _fetch(signal_id: str) -> dict:
    signals = {signal["id"]: signal for signal in yaml.safe_load(_block())["signals"]}
    return signals[signal_id]["fetch"]


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # what expanduser reads on Windows
    return tmp_path


def test_block_hard_codes_no_drive_or_owner_home():
    # `http:/` isn't a drive: the letter before the colon has to stand alone.
    assert not re.findall(r"\b[A-Za-z]:[\\/]|/home/|/Users/", _block())


def test_paths_are_where_the_installer_and_the_daemon_put_the_files():
    assert _fetch("canary")["path"] == _DB
    assert _fetch("daemon-errors")["path"] == _LOG

    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8").splitlines()
    for default in ('install_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/agent-harness',
                    'data_dir=${data_dir:-"$install_dir/data"}',
                    'log_dir="$install_dir/logs"'):
        assert default in installer
    supervisor = (ROOT / "install" / "run-daemon.sh").read_text(encoding="utf-8").splitlines()
    assert 'daemon_log="$log_dir/daemon.log"' in supervisor
    assert Config.db_path.fget(SimpleNamespace(data_dir=Path("data"))) == Path("data") / "harness.sqlite3"


def test_paths_expand_under_the_home_of_whoever_runs_the_sweep(home):
    assert Path(_expand(_fetch("canary")["path"])) == home / ".local/share/agent-harness/data/harness.sqlite3"
    assert Path(_expand(_fetch("daemon-errors")["path"])) == home / ".local/share/agent-harness/logs/daemon.log"


def test_canary_query_reads_a_real_store_at_that_path_read_only(home):
    fetch = _fetch("canary")
    path = Path(_expand(fetch["path"]))
    db = Database(path)
    db.conn.execute("INSERT INTO canary_results (sha, started_at, finished_at, status) VALUES ('a', 1, 2, 'running')")
    db.conn.execute("INSERT INTO canary_results (sha, started_at, finished_at, status) VALUES ('b', 1, 3, 'complete')")
    db.close()

    reader = sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro", uri=True)
    try:
        assert reader.execute(fetch["query"]).fetchone() == (3,)
    finally:
        reader.close()


def test_canary_hint_names_a_gauge_the_daemon_exports():
    block = yaml.safe_load(_block())
    hint = next(signal["hint"] for signal in block["signals"] if signal["id"] == "canary")
    gauge = re.search(r"harness_\w+", hint)
    assert gauge, hint
    assert f'"{gauge.group()}"' in (ROOT / "harness" / "metrics.py").read_text(encoding="utf-8")
