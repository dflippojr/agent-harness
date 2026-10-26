"""Issue #726: the always-on `gate` check, untested-main fix and deploy order.

scripts/ci_gate.py makes the decisions; ci.yml and ci-cd.yml only wire them. These tests cover both halves.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import ci_gate  # noqa: E402

ROOT = Path(__file__).parents[1]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CI_CD = yaml.safe_load((ROOT / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8"))
SKIPPED = {"LICENSE", "docs/phase0-results.md"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "ci")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "harness.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("license\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _runner_in(repo: Path):
    def run(command: list[str]) -> str:
        if command[:2] == ["git", "fetch"]:
            return ""  # the commit is already local; CI fetches it from origin
        return subprocess.run(["git", "-C", str(repo), *command[1:]], check=True, capture_output=True,
                              text=True).stdout
    return run


# --- changes ---------------------------------------------------------------------------------------------------

def test_docs_only_change_skips_tests_and_code_change_runs_them():
    assert ci_gate.decide_tests(["LICENSE", "docs/phase0-results.md"], SKIPPED)[0] is False
    tests, reason = ci_gate.decide_tests(["LICENSE", "harness/app.py"], SKIPPED)
    assert tests is True and "harness/app.py" in reason


@pytest.mark.parametrize("changed", [None, []])
def test_unknown_or_empty_change_list_runs_the_tests(changed):
    assert ci_gate.decide_tests(changed, SKIPPED)[0] is True


def test_skip_list_is_exact_paths():
    skipped = ci_gate.skip_paths("LICENSE\n\n  docs/phase0-results.md  \n")
    assert skipped == SKIPPED
    # No glob or prefix matching: a sibling or nested path still runs the tests.
    assert ci_gate.files_needing_tests(["docs/phase0-results.md.bak", "x/LICENSE"], skipped) == [
        "docs/phase0-results.md.bak", "x/LICENSE"]


def test_pull_request_diffs_the_merge_commit_against_its_base_parent(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "LICENSE").write_text("changed\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "docs only")
    _git(repo, "checkout", "-q", "main")
    (repo / "harness.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "main moved on with code")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    assert base != _git(repo, "rev-parse", "HEAD^1")
    # Only what merging the PR changes, not what main did meanwhile.
    assert ci_gate.changed_files("pull_request", "", _runner_in(repo)) == ["LICENSE"]


def test_push_diffs_against_the_event_before_commit_without_rename_detection(repo):
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "harness.py", "LICENSE2")
    _git(repo, "commit", "-q", "-m", "rename")
    changed = ci_gate.changed_files("push", before, _runner_in(repo))
    assert changed is not None
    # With rename detection on, git would list only the new path and hide harness.py.
    assert sorted(changed) == ["LICENSE2", "harness.py"]


def test_push_without_a_usable_before_commit_lists_nothing(repo):
    assert ci_gate.changed_files("push", ci_gate.ZERO_SHA, _runner_in(repo)) is None
    assert ci_gate.changed_files("push", "", _runner_in(repo)) is None

    def failing_fetch(command):
        raise subprocess.CalledProcessError(128, command)
    assert ci_gate.changed_files("push", "a" * 40, failing_fetch) is None
    assert ci_gate.changed_files("push", "b" * 40, _runner_in(repo)) is None  # unknown commit: diff fails


def test_changes_command_writes_the_output(repo, monkeypatch, tmp_path_factory):
    output = tmp_path_factory.mktemp("out") / "github_output"
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "LICENSE").write_text("docs\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "docs")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci_gate, "_run", _runner_in(repo))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("PUSH_BEFORE", before)
    monkeypatch.setenv("CI_SKIP_UNREAD_PATHS", "LICENSE\n")
    assert ci_gate.main(["changes"]) == 0
    assert output.read_text(encoding="utf-8") == "tests=false\n"


# --- gate ------------------------------------------------------------------------------------------------------

def _needs(tests: str = "true", changes: str = "success", **results: str) -> dict:
    needs = {"changes": {"result": changes, "outputs": {"tests": tests}}}
    for job in ("lint", "test", "test-py313", "review-linux", "sonar", "openapi", "ui-tests", "hub-contract"):
        needs[job] = {"result": results.get(job.replace("-", "_"), "success"), "outputs": {}}
    return needs


def test_gate_passes_when_every_job_passed():
    assert ci_gate.gate_failures(_needs()) == []


def test_gate_passes_a_docs_only_change_with_tests_skipped():
    needs = _needs("false", test="skipped", test_py313="skipped", review_linux="skipped", sonar="skipped",
                   openapi="skipped", ui_tests="skipped", hub_contract="skipped")
    assert ci_gate.gate_failures(needs) == []


@pytest.mark.parametrize(
    "job", ["lint", "test", "test_py313", "review_linux", "openapi", "ui_tests", "hub_contract", "sonar"])
@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_gate_fails_when_a_pull_request_job_failed_or_was_cancelled(job, result):
    failures = ci_gate.gate_failures(_needs(**{job: result}))
    assert failures == [f"{job.replace('_', '-')}: {result}"]


def test_gate_fails_when_tests_were_wanted_but_skipped():
    assert ci_gate.gate_failures(_needs(test="skipped")) == [
        "test: skipped although changed files need the tests"]


def test_gate_fails_when_lint_was_skipped_even_on_a_docs_only_change():
    needs = _needs("false", lint="skipped", test="skipped", review_linux="skipped", sonar="skipped",
                   openapi="skipped", ui_tests="skipped", hub_contract="skipped")
    assert ci_gate.gate_failures(needs) == ["lint: skipped although it runs on every change"]


def test_gate_fails_when_changes_failed():
    needs = _needs("", changes="failure", test="skipped", test_py313="skipped", review_linux="skipped",
                   sonar="skipped", openapi="skipped", ui_tests="skipped", hub_contract="skipped")
    failures = ci_gate.gate_failures(needs)
    assert failures[0] == "changes: failure"
    assert len(failures) == 7  # and each test job skipped without a decision


def test_gate_fails_on_a_failed_sonar():
    # A failing quality gate blocks main pushes as it does pull requests (#728); the gate no longer reads the event.
    assert ci_gate.gate_failures(_needs(sonar="failure")) == ["sonar: failure"]
    assert ci_gate.gate_failures(_needs(sonar="skipped")) == []  # forks have no token


@pytest.mark.parametrize("job", ["ui-tests", "hub-contract", "lint", "test-py313", "review-linux"])
def test_gate_fails_when_a_required_job_is_missing_from_needs(job):
    needs = _needs()
    del needs[job]
    assert ci_gate.gate_failures(needs) == [f"{job}: not in gate's needs"]


def test_gate_command_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("NEEDS", json.dumps(_needs()))
    assert ci_gate.main(["gate"]) == 0
    monkeypatch.setenv("NEEDS", json.dumps(_needs(test="failure")))
    assert ci_gate.main(["gate"]) == 1
    assert "::error::gate: test: failure" in capsys.readouterr().out


# --- deploy guard ----------------------------------------------------------------------------------------------

def _gh(test_conclusion: str | None, tip: str):
    def run(command):
        assert command[:2] == ["gh", "api"]
        if command[2].endswith("/jobs?per_page=100"):
            jobs = [{"name": "changes", "conclusion": "success"}, {"name": "gate", "conclusion": "success"}]
            if test_conclusion is not None:
                jobs.append({"name": "test", "conclusion": test_conclusion})
            return json.dumps({"jobs": jobs})
        assert command[2] == "repos/o/r/git/ref/heads/main"
        return tip + "\n"
    return run


def test_deploy_allows_a_tested_tip():
    assert ci_gate.deploy_decision("o/r", "1", "a" * 40, _gh("success", "a" * 40))[0] is True


@pytest.mark.parametrize("conclusion", ["skipped", "failure", None])
def test_deploy_refuses_a_run_whose_test_job_did_not_pass(conclusion):
    allowed, reason = ci_gate.deploy_decision("o/r", "1", "a" * 40, _gh(conclusion, "a" * 40))
    assert allowed is False and "docs-only" in reason


def test_deploy_refuses_an_older_commit_finishing_after_a_newer_one():
    allowed, reason = ci_gate.deploy_decision("o/r", "1", "a" * 40, _gh("success", "b" * 40))
    assert allowed is False and "superseded" in reason


def test_deploy_command_writes_the_output(monkeypatch, tmp_path):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(ci_gate, "_run", _gh("success", "c" * 40))
    assert ci_gate.main(["deploy", "--repository", "o/r", "--run-id", "1", "--head-sha", "c" * 40]) == 0
    assert output.read_text(encoding="utf-8") == "deploy=true\n"


# --- workflow wiring -------------------------------------------------------------------------------------------

def test_gate_always_runs_and_needs_every_other_job():
    jobs = CI["jobs"]
    gate = jobs["gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == set(jobs) - {"gate"}
    assert "scripts/ci_gate.py gate" in gate["steps"][-1]["run"]
    assert gate["steps"][-1]["env"]["NEEDS"] == "${{ toJSON(needs) }}"


def test_test_jobs_skip_only_on_the_changes_decision():
    for job in ci_gate.TEST_JOBS:
        assert CI["jobs"][job]["needs"] == "changes", job
        assert CI["jobs"][job]["if"] == "needs.changes.outputs.tests == 'true'", job
    assert CI["jobs"]["sonar"]["needs"] == "test"


def test_main_pushes_never_cancel_or_replace_each_other():
    concurrency = CI["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    # A shared group would drop a pending main run when a newer one queues, so each main push groups on its commit.
    assert concurrency["group"] == "ci-${{ github.event_name == 'pull_request' && github.ref || github.sha }}"


def test_coverage_has_a_floor():
    run = next(s for s in CI["jobs"]["test"]["steps"] if s.get("name") == "Run tests with coverage")["run"]
    assert "--cov-fail-under=88" in run
    assert CI_TEXT.count("--cov-fail-under") == 1


def test_publish_and_deploy_wait_for_the_guard():
    jobs = CI_CD["jobs"]
    assert "scripts/ci_gate.py deploy" in jobs["guard"]["steps"][-1]["run"]
    assert jobs["guard"]["permissions"] == {"actions": "read", "contents": "read"}
    assert jobs["publish-images"]["needs"] == "guard"
    assert jobs["deploy-tower"]["needs"] == ["guard", "publish-images"]
    for name in ("publish-images", "deploy-tower"):
        assert "&& needs.guard.outputs.deploy == 'true'" in " ".join(jobs[name]["if"].split()), name


def test_guard_runs_outside_any_concurrency_group():
    # A workflow-level group would let an older CI completion replace the newest commit's pending run before its
    # guard ran (GitHub replaces a group's pending run when another queues). Only jobs after `guard` serialize.
    assert "concurrency" not in CI_CD
    jobs = CI_CD["jobs"]
    assert "concurrency" not in jobs["guard"]
    groups = {name: jobs[name]["concurrency"] for name in ("publish-images", "deploy-tower")}
    assert groups["publish-images"]["group"] != groups["deploy-tower"]["group"]
    assert all(group["cancel-in-progress"] is False for group in groups.values())


def test_publish_rechecks_the_tip_inside_its_group_before_any_image_step():
    publish = CI_CD["jobs"]["publish-images"]
    steps = publish["steps"]
    fresh_at = next(i for i, step in enumerate(steps) if step.get("id") == "fresh")
    assert "scripts/ci_gate.py deploy" in steps[fresh_at]["run"]
    assert all("docker/" not in step.get("uses", "") for step in steps[:fresh_at])
    for step in steps[fresh_at + 1:]:
        assert step["if"] == "steps.fresh.outputs.deploy == 'true'", step
    assert publish["outputs"]["published"] == "${{ steps.fresh.outputs.deploy }}"
    assert publish["permissions"]["actions"] == "read"
    deploy_if = " ".join(CI_CD["jobs"]["deploy-tower"]["if"].split())
    assert "&& needs.publish-images.outputs.published == 'true'" in deploy_if

