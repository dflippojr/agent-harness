"""Doctor, clone and runner support for GitHub-sourced tasks."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from harness import doctor, projects
from harness.config import Project
from harness.projects import GitError


class Rec(doctor.Report):
    def __init__(self):
        super().__init__()
        self.lines = []

    def ok(self, name, detail=""):
        self.lines.append(("ok", name, detail))

    def warn(self, name, detail):
        super().warn(name, detail)
        self.lines.append(("warn", name, detail))


def test_doctor_github_token_states(tmp_path):
    cfg = SimpleNamespace(data_dir=tmp_path, github=SimpleNamespace(token_file=""))
    r = Rec()
    doctor.check_github_token(r, cfg)
    assert r.lines[-1][0] == "ok" and "not configured" in r.lines[-1][2]
    token = tmp_path / "tok"
    token.write_text("secret-value")
    token.chmod(0o600)
    cfg.github.token_file = str(token)
    doctor.check_github_token(r, cfg)
    assert r.lines[-1][0] == "ok" and "readable" in r.lines[-1][2]
    cfg.github.token_file = str(tmp_path / "missing")
    doctor.check_github_token(r, cfg)
    assert r.lines[-1][0] == "warn"
    assert all("secret-value" not in d and "missing" not in d for _, _, d in r.lines)


def test_doctor_main_runs_github_check(monkeypatch, capsys):
    from harness import config as config_mod
    cfg = SimpleNamespace(github=SimpleNamespace(token_file=""), profile="p", default_model="m", port=1,
                          modules=SimpleNamespace(local_model=False))
    monkeypatch.setattr(config_mod, "load", lambda d=None: cfg)
    seen = []
    for name in dir(doctor):
        if name.startswith("check_") and name != "check_github_token":
            monkeypatch.setattr(doctor, name, lambda *a, **k: None)
    monkeypatch.setattr(doctor, "check_github_token", lambda r, c: seen.append(c))
    try:
        doctor.main([])
    except SystemExit:
        pass
    assert seen == [cfg]


def test_prepare_reports_deleted_base_branch(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for cmd in (["init", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=src, check=True, capture_output=True)
    project = Project(name="p", repo=str(src), base_branch="gone")
    with pytest.raises(GitError, match="gone was deleted or is inaccessible"):
        projects.prepare(project, tmp_path / "ws", "sid")
    assert not any((tmp_path / "ws").iterdir())


def test_first_prepare_applies_github_base_branch(monkeypatch, tmp_path):
    from harness.runner import Runner
    got = {}
    monkeypatch.setattr(projects, "prepare", lambda project, ws, sid: got.update(branch=project.base_branch) or {})
    s = {"id": "s1", "app_metadata": {"github_base_branch": "feat/x"}}
    asyncio.run(Runner._first_prepare(SimpleNamespace(), s, Project(name="p", base_branch="main"), tmp_path, False, False))
    assert got["branch"] == "feat/x"
