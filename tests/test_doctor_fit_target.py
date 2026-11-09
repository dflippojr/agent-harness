"""doctor warns on GPU guard settings llama-server's --fit-target can't honour (#832, VRAM budget follow-up 6).

The margin is read from the llama unit install/install.sh writes; no GPU, daemon or real unit is touched."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import doctor
from harness.config import GpuGuardConfig
from harness_modules.local_model import doctor as local_doctor

CHECK = local_doctor.FIT_CHECK
# The installer's form: every argument double-quoted, `"` and `\` backslash-escaped, `%` doubled (systemd_escape).
UNIT = """[Unit]
Description=Agent harness llama.cpp server (Main)

[Service]
Type=simple
Environment="HARNESS_LOG_DIR=/home/u/agent harness/logs"
ExecStart=/bin/bash "/home/u/agent harness/bin/run-server.sh" "Main" "ghcr.io/ggml-org/llama.cpp:server-cuda-b10830" \
"/models/50%% \\"q4\\".gguf" "qwen3.6-35b-a3b" "8090" "65536" "/home/u/agent harness/llama.paused"{extra}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""
QWEN = ' "--fit" "on" "--cache-type-k" "q8_0" "--cache-type-v" "q8_0"'


def _cfg(enabled=True, allowed=0, floor=0, local_model=True):
    return SimpleNamespace(modules=SimpleNamespace(local_model=local_model),
                           gpu_guard=GpuGuardConfig(enabled=enabled, plex_transcodes_allowed=allowed,
                                                    min_free_vram_mib=floor))


@pytest.fixture
def units(tmp_path, monkeypatch):
    """The user's systemd unit directory, under tmp_path; the check runs as on Linux."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(local_doctor.sys, "platform", "linux")
    path = tmp_path / "config/systemd/user"
    path.mkdir(parents=True)
    return path


def _write(units: Path, fit: str | None, instance: str = "main") -> Path:
    extra = QWEN + (f' "--fit-target" "{fit}"' if fit is not None else "")
    unit = units / f"agent-harness-{instance}-llama.service"
    unit.write_text(UNIT.replace("\\\n", "").format(extra=extra))
    return unit


def _run(cfg, capsys, **report):
    r = doctor.Report(**report)
    local_doctor.check_fit_target(r, cfg)
    return r, [line for line in capsys.readouterr().out.splitlines() if CHECK in line]


def test_the_parser_reads_the_installers_quoted_exec_start(units):
    unit = _write(units, "2048")
    assert local_doctor.read_fit_target(unit) == 2048
    assert local_doctor.read_fit_target(_write(units, None)) is None


def test_the_parser_accepts_the_equals_form_and_rejects_a_non_number(units):
    unit = units / "agent-harness-main-llama.service"
    unit.write_text(UNIT.replace("\\\n", "").format(extra=QWEN + ' "--fit-target=1700"'))
    assert local_doctor.read_fit_target(unit) == 1700
    unit.write_text(UNIT.replace("\\\n", "").format(extra=QWEN + ' "--fit-target" "lots"'))
    with pytest.raises(ValueError, match="lots"):
        local_doctor.read_fit_target(unit)


@pytest.mark.parametrize("allowed, fit", [(1, "1456"), (1, "2048"), (2, "2156"), (4, "3556")])
def test_a_margin_at_or_above_the_plex_minimum_does_not_warn(units, capsys, allowed, fit):
    _write(units, fit)
    r, lines = _run(_cfg(allowed=allowed), capsys)
    assert r.warned == 0 and r.failed == 0
    assert len(lines) == 1 and "[ OK ]" in lines[0] and f"--fit-target {fit} in " in lines[0]


def test_a_margin_below_the_plex_minimum_warns_with_setting_found_and_needed(units, capsys):
    _write(units, "1455")
    r, lines = _run(_cfg(allowed=1), capsys)
    assert r.warned == 1
    (line,) = lines
    assert "[WARN]" in line and "gpu_guard.plex_transcodes_allowed 1" in line
    assert "--fit-target 1455" in line and "at least 1456 MiB" in line and "700 × 1 + 500 + 256" in line


def test_a_missing_margin_warns_for_plex(units, capsys):
    unit = _write(units, None)
    r, lines = _run(_cfg(allowed=2), capsys)
    assert r.warned == 1
    (line,) = lines
    assert "gpu_guard.plex_transcodes_allowed 2" in line and "at least 2156 MiB" in line
    assert f"{unit} sets no --fit-target" in line and "default 1024" in line


@pytest.mark.parametrize("fit, floor", [("2048", 2048), ("2048", 3000), (None, 1024), (None, 1500)])
def test_a_floor_not_below_the_margin_warns_that_the_guard_parks_after_every_load(units, capsys, fit, floor):
    _write(units, fit)
    r, lines = _run(_cfg(floor=floor), capsys)
    assert r.warned == 1
    (line,) = lines
    assert f"gpu_guard.min_free_vram_mib {floor}" in line
    assert ("--fit-target 2048" if fit else "--fit-target 1024 (llama-server's default") in line
    assert "the guard would park the model right after every load" in line


@pytest.mark.parametrize("fit, floor", [("2048", 2047), (None, 1023), (None, 500)])
def test_a_floor_below_the_margin_does_not_warn(units, capsys, fit, floor):
    _write(units, fit)
    r, lines = _run(_cfg(floor=floor), capsys)
    assert r.warned == 0 and len(lines) == 1 and "[ OK ]" in lines[0]


def test_both_warnings_together(units, capsys):
    _write(units, "1024")
    r, lines = _run(_cfg(allowed=1, floor=1200), capsys)
    assert r.warned == 2 and len(lines) == 2
    assert "plex_transcodes_allowed" in lines[0] and "min_free_vram_mib" in lines[1]


@pytest.mark.parametrize("cfg", [_cfg(enabled=False, allowed=1, floor=4000), _cfg(allowed=0, floor=0),
                                 _cfg(local_model=False, allowed=1, floor=4000)],
                         ids=["guard-off", "both-zero", "no-local-model"])
def test_nothing_is_said_when_the_guard_is_off_or_both_settings_are_zero(units, capsys, cfg):
    _write(units, "100")  # a margin that would fail both checks if they ran
    r, lines = _run(cfg, capsys)
    assert r.warned == 0 and lines == []


def test_no_unit_says_the_check_was_not_possible_and_warns_about_no_value(units, capsys):
    r, lines = _run(_cfg(allowed=1, floor=4000), capsys)
    assert r.warned == 0 and r.failed == 0
    (line,) = lines
    assert "[ OK ]" in line and "not checked" in line and "agent-harness-main-llama.service" in line
    assert "plex_transcodes_allowed" not in line and "min_free_vram_mib" not in line


def test_an_unreadable_value_is_not_checked_rather_than_treated_as_missing(units, capsys):
    _write(units, "lots")
    r, lines = _run(_cfg(allowed=1), capsys)
    assert r.warned == 0
    (line,) = lines
    assert "not checked" in line and "lots" in line


def test_the_instance_picks_the_unit(units, capsys):
    _write(units, "100")  # main's unit would warn
    _write(units, "2048", instance="lab")
    r, lines = _run(_cfg(allowed=1), capsys, instance="Lab")
    assert r.warned == 0 and "agent-harness-lab-llama.service" in lines[0]


@pytest.mark.parametrize("report, platform", [({"existing_server": True}, "linux"), ({}, "win32"), ({}, "darwin")],
                         ids=["existing-server", "windows", "macos"])
def test_no_installer_unit_means_not_checked(units, capsys, monkeypatch, report, platform):
    _write(units, "100")
    monkeypatch.setattr(local_doctor.sys, "platform", platform)
    r, lines = _run(_cfg(allowed=1, floor=4000), capsys, **report)
    assert r.warned == 0
    (line,) = lines
    assert "not checked" in line


def test_the_check_runs_even_when_services_were_not_started(units, capsys, monkeypatch):
    _write(units, "1024")
    monkeypatch.setattr(local_doctor, "check_gpu", lambda r, cfg: None)
    r = doctor.Report(not_started=True)
    local_doctor.run(r, _cfg(allowed=1))
    out = capsys.readouterr().out
    assert "plex_transcodes_allowed 1" in out and "Local model services" in out


def test_main_hands_instance_and_existing_server_to_the_module_checks(monkeypatch):
    reports, real = [], doctor.Report
    monkeypatch.setattr(doctor, "Report", lambda **kw: reports.append(real(**kw)) or reports[-1])
    from harness import config as config_mod
    monkeypatch.setattr(config_mod, "load", lambda _d: (_ for _ in ()).throw(RuntimeError("stop here")))
    assert doctor.main(["--instance", "Lab", "--existing-server"]) == 1
    assert reports[0].instance == "Lab" and reports[0].existing_server is True
