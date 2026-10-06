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
    # A failing quality gate on main shows on the job but never blocks the deploy; failing tests still do.
    assert "continue-on-error: ${{ github.event_name == 'push' }}" in sonar_job
    assert "runs-on: [self-hosted" not in workflow
    assert "runs-on: windows-latest" in workflow
    assert "runs-on: ubuntu-latest" not in workflow
    assert "sonar.qualitygate.wait=true" in workflow
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest-xdist" not in requirements
    assert "pytest-cov" not in requirements


def test_sonar_properties_feed_coverage_into_the_quality_gate():
    properties = PROPERTIES.read_text(encoding="utf-8")
    assert "sonar.python.coverage.reportPaths=coverage.xml" in properties
    assert "sonar.coverage.exclusions=**/*" not in properties
    assert "harness/web/**" in properties
