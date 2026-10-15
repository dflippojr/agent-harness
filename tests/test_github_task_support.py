"""Doctor, clone and runner support for GitHub-sourced tasks."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import doctor, projects
from harness.config import Project
from harness.llm import Completion
from harness.manager import Manager
from harness.projects import GitError
from test_daemon import Script
from test_phase3 import finished, make_repo, project_cfg, sh


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


def test_prepare_reports_deleted_base_branch(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for cmd in (["init", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=src, check=True, capture_output=True)
    project = Project(name="p", repo=str(src), base_branch="gone")
    with pytest.raises(GitError, match="gone was deleted or is inaccessible"):
        projects.prepare(project, tmp_path / "ws", "sid")
    assert not any((tmp_path / "ws").iterdir())


def test_first_prepare_applies_github_base_branch(tmp_path):
    src = make_repo(tmp_path / "src")
    main_head = projects.head(src)
    sh(src, "checkout", "-q", "-b", "feat/x")
    (src / "app.py").write_text("VALUE = 'from the PR branch'\n")
    sh(src, "commit", "-qam", "PR work")
    feature_head = projects.head(src)
    sh(src, "checkout", "-q", "main")
    cfg = project_cfg(tmp_path, str(src))

    async def body():
        m = Manager(cfg, chat=Script([Completion(content="Looked."), Completion(content="Looked again.")]))
        await m.start(maintenance=False)
        plain = await finished(m, m.create("look", project="proj")["id"])
        assert plain["base_commit"] == main_head
        s = await finished(m, m.create("look", project="proj",
                                       app_metadata={"github_base_branch": "feat/x"})["id"])
        assert s["base_commit"] == feature_head and s["base_branch"] == "feat/x"
        assert (Path(s["workspace"]) / "app.py").read_text() == "VALUE = 'from the PR branch'\n"
        await m.stop()
    asyncio.run(body())
