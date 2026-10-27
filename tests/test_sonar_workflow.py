"""Contracts for SonarCloud coverage reporting and the quality-gate wait."""

from pathlib import Path
import os
import subprocess

import pytest
import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PROPERTIES = ROOT / "sonar-project.properties"


@pytest.mark.parametrize(
    ("token", "dependabot", "available", "exit_code"),
    [("test-placeholder", "true", "true", 0),
     ("test-placeholder", "false", "true", 0),
     ("", "true", "false", 0),
     ("   ", "true", "false", 0),
     ("", "false", None, 1)],
)
def test_sonar_token_preflight(tmp_path, token, dependabot, available, exit_code):
    """Execute the workflow's preflight with synthetic inputs, never real secrets."""
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["sonar"]
    preflight = next(step for step in job["steps"] if step.get("id") == "sonar-token")
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    env = {**os.environ, "SONAR_TOKEN": token, "DEPENDABOT_PR": dependabot,
           "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", preflight["run"]],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == exit_code
    if available is None:
        assert not output.exists()
        assert "required for SonarCloud analysis" in result.stderr
    else:
        assert output.read_text(encoding="utf-8-sig").strip() == f"available={available}"
    if available == "false":
        assert "::notice::" in result.stdout
        assert "analysis was not performed" in summary.read_text(encoding="utf-8-sig")
    else:
        assert not summary.exists()
    assert "test-placeholder" not in result.stdout + result.stderr


def test_sonar_uses_same_scan_for_dependabot_and_preserves_fork_guard():
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["sonar"]
    assert job["if"] == (
        "github.event_name == 'push' || "
        "github.event.pull_request.head.repo.full_name == github.repository"
    )
    preflight = next(step for step in job["steps"] if step.get("id") == "sonar-token")
    scan = next(step for step in job["steps"] if step.get("name") == "Scan with SonarCloud")
    assert preflight["env"]["SONAR_TOKEN"] == scan["env"]["SONAR_TOKEN"] == "${{ secrets.SONARCLOUD_TOKEN }}"
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in preflight["env"]["DEPENDABOT_PR"]
    assert scan["if"] == "steps.sonar-token.outputs.available == 'true'"
    assert "continue-on-error" not in scan


def test_sonar_job_produces_coverage_xml_before_the_scan():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    coverage_at = workflow.index("--cov-report=xml:coverage.xml")
    scan_at = workflow.index("SonarSource/sonarqube-scan-action")
    assert coverage_at < scan_at
    assert "pytest-cov" in workflow
    assert "pytest-xdist" in workflow
    assert "python -m pytest tests" in workflow
    # Two suite runs: `test` (3.12) is the coverage run; `test-py313` runs the suite again without coverage (#730).
    assert workflow.count("python -m pytest tests -q -n") == 2
    assert workflow.count("--cov-report=xml:coverage.xml") == 1
    assert "-n 4 --dist loadfile" in workflow  # the verbosity flag is free to change; the worker split is not
    coverage_step = workflow[workflow.index("Run tests with coverage") : workflow.index("Scan with SonarCloud")]
    assert "-n auto" not in coverage_step
    assert "COVERAGE_CORE" not in coverage_step
    assert "NUMBER_OF_PROCESSORS" in workflow
    assert "jobs:\n  test:" in workflow
    sonar_job = workflow[workflow.index("\n  sonar:\n") :]
    assert "needs: test" in sonar_job
    assert "actions/download-artifact" in sonar_job
    assert "fetch-depth: 0" in sonar_job
    # A failing quality gate blocks on main as it does on pull requests, so it stops the deploy (#728).
    assert "continue-on-error" not in yaml.safe_load(workflow)["jobs"]["sonar"]
    assert "runs-on: [self-hosted" not in workflow
    assert "runs-on: windows-latest" in workflow
    # Only the bookkeeping jobs (`changes`, `gate`, #726), the static checks (`lint`, #729), the Hub's contract suite
    # (`hub-contract`, #736) and the Linux deployment tests (`deploy-linux`, #812) may use Ubuntu; the suite and its
    # analysis cannot. `hub-contract` runs the Hub's tests and `deploy-linux` one module that skips on Windows; neither
    # writes coverage. `gate-guard` (#727) calls the shared guard, whose diff check runs on the called workflow's
    # ubuntu-latest default, so it has no runs-on of its own.
    jobs = yaml.safe_load(workflow)["jobs"]
    for job in ("test", "test-py313", "sonar", "openapi", "ui-tests"):
        assert jobs[job]["runs-on"] == "windows-latest", job
    ubuntu = {name for name, job in jobs.items() if job.get("runs-on") == "ubuntu-latest"}
    assert ubuntu == {"changes", "gate", "lint", "hub-contract", "deploy-linux"}
    for job in ("hub-contract", "deploy-linux"):
        assert "--cov" not in yaml.safe_dump(jobs[job]), job
    assert "uses" in jobs["gate-guard"] and "runs-on" not in jobs["gate-guard"]
    assert "sonar.qualitygate.wait=true" in workflow
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest-xdist" not in requirements
    assert "pytest-cov" not in requirements
    assert "pytest>=" in requirements


def test_sonar_properties_feed_coverage_into_the_quality_gate():
    properties = PROPERTIES.read_text(encoding="utf-8")
    assert "sonar.python.coverage.reportPaths=coverage.xml" in properties
    assert "sonar.coverage.exclusions=**/*" not in properties
    assert "**/*.mjs" in properties
