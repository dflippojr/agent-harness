"""Issue #236: lock the #221/#224 CLI protections so removing any of them fails a test."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from harness import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".agent-harness"
    monkeypatch.setattr(cli, "HARNESS_HOME", home)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", home / "client" / "config.json")
    monkeypatch.setattr(cli, "DEFAULT_RUNNER_CONFIG", home / "runner" / "config.json")
    monkeypatch.delenv("HARNESS_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    return home


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(cli.sys, "argv", ["harness", *argv])
    return cli.main()


def refuse(monkeypatch, capsys, *argv):
    with pytest.raises(SystemExit) as exit_:
        run_main(monkeypatch, *argv)
    assert exit_.value.code == 2
    assert "must be a file under" in capsys.readouterr().err


def symlink_or_skip_platform(link, target, **kw):
    try:
        link.symlink_to(target, **kw)
    except (OSError, NotImplementedError) as exc:
        if os.name == "nt" and kw.get("target_is_directory"):   # a junction resolves like a directory link
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
            return
        pytest.xfail(f"symlinks unavailable on this platform: {exc}")


@pytest.mark.parametrize("flag", ["--config", "--runner-config"])
def test_config_flags_reject_dotdot_and_absolute_escapes(home, tmp_path, monkeypatch, capsys, flag):
    home.mkdir(parents=True)
    escapes = [home / ".." / "x.json", home / "client" / ".." / ".." / "x.json", tmp_path / "abs.json",
               home.parent / f"{home.name}-sibling" / "x.json"]
    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: pytest.fail("no request before validation"))
    for value in escapes:
        argv = ["--config", str(value), "queue"] if flag == "--config" else [
            "pair", "https://t.example", "c", "--runner-config", str(value)]
        refuse(monkeypatch, capsys, *argv)
    assert not (tmp_path / "abs.json").exists() and not (home.parent / "x.json").exists()


def test_config_flags_reject_the_home_directory_itself(home, monkeypatch, capsys):
    home.mkdir(parents=True)
    refuse(monkeypatch, capsys, "--config", str(home), "queue")


def test_config_flags_reject_symlink_escapes(home, tmp_path, monkeypatch, capsys):
    home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip_platform(home / "link", outside, target_is_directory=True)
    refuse(monkeypatch, capsys, "--config", str(home / "link" / "c.json"), "queue")
    refuse(monkeypatch, capsys, "projects", "add", str(tmp_path), "--runner-config", str(home / "link" / "r.json"))
    assert not list(outside.iterdir())


def test_config_flags_accept_paths_inside_home(home, monkeypatch):
    monkeypatch.setattr(cli, "api", lambda *a, **k: [])
    inside = home / "sub" / "c.json"
    assert run_main(monkeypatch, "--config", str(inside), "queue") == 0
    assert cli.CONFIG_PATH == inside.resolve()


def test_validation_runs_before_any_network_or_write(home, tmp_path, monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(cli.httpx, "post", lambda *a, **k: posted.append(a))
    outside = tmp_path / "out.json"
    refuse(monkeypatch, capsys, "pair", "s", "c", "--runner-config", str(outside))
    assert posted == [] and not outside.exists() and not home.exists()


def test_credentials_are_created_fresh_and_private_before_any_token_is_written(tmp_path, monkeypatch):
    target = tmp_path / "client" / "config.json"
    opened, sizes = [], []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, **kw):
        fd = real_open(path, flags, mode, **kw)
        opened.append((os.fspath(path), flags, mode))
        sizes.append(os.path.getsize(path))   # still empty when it is created
        return fd

    monkeypatch.setattr(cli.os, "open", spy_open)
    cli._write_private_json(target, {"token": "ho-secret"})
    assert len(opened) == 1
    path, flags, mode = opened[0]
    assert path.endswith("config.json.new")
    assert flags & os.O_EXCL and flags & os.O_CREAT and mode == 0o600
    assert sizes == [0]
    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "ho-secret"}
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_credentials_replace_a_symlink_at_the_temp_name_without_following_it(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    target = tmp_path / "client" / "config.json"
    target.parent.mkdir()
    symlink_or_skip_platform(target.with_name("config.json.new"), victim)
    cli._write_private_json(target, {"token": "ho-secret"})
    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "ho-secret"}


def test_existing_config_survives_a_failed_write(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_text('{"token": "old"}', encoding="utf-8")
    monkeypatch.setattr(cli.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        cli._write_private_json(target, {"token": "new"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "old"}


def test_runner_logs_are_bounded_and_read_only_from_the_fixed_log(home, monkeypatch, capsys):
    log = home / "logs" / "runner.log"
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line{i}\n" for i in range(5000)), encoding="utf-8")
    other = home / "runner" / "config.json"
    other.parent.mkdir(parents=True)
    other.write_text('{"server": "http://unused.example"}', encoding="utf-8")
    for name in ("run", "call", "Popen", "check_output"):
        monkeypatch.setattr(cli.subprocess, name, lambda *a, **k: pytest.fail("no subprocess for logs"))
    monkeypatch.setattr(cli.os, "system", lambda *a: 0)
    run_main(monkeypatch, "--config", str(other), "runner", "logs", "--lines", "3")
    assert capsys.readouterr().out == "line4997\nline4998\nline4999\n"
    for lines in ("-5", "0"):
        run_main(monkeypatch, "runner", "logs", "--lines", lines)
        assert capsys.readouterr().out == "line4999\n"   # never an unbounded dump


def test_runner_log_tail_is_capped_in_memory(tmp_path, monkeypatch):
    log = tmp_path / "runner.log"
    log.write_text("a\nb\nc\nd\n", encoding="utf-8")
    kept = []
    real_deque = cli.deque

    def spy(iterable, maxlen=None):
        kept.append(maxlen)
        return real_deque(iterable, maxlen=maxlen)

    monkeypatch.setattr(cli, "deque", spy)
    assert cli._show_runner_logs(log, 2, False) == 0
    assert kept == [2]
