"""Contracts for SonarCloud coverage reporting and the quality-gate wait."""

from pathlib import Path
import os
import shutil
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
        # The job runs it under Windows PowerShell; PowerShell 7 runs the same script where that is absent (Linux).
        [shutil.which("powershell") or "pwsh", "-NoProfile", "-NonInteractive", "-Command", preflight["run"]],
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
    jobs = yaml.safe_load(workflow)["jobs"]
    # Two coverage runs, Windows (`test`) and Linux (`test-linux`, #973), each uploading its .coverage data; the
    # `coverage` job combines them and writes the one coverage.xml before the scan; `test-py313` runs without it (#730).
    assert workflow.count("python -m pytest tests -q -n") == 3
    assert "-n 4 --dist loadfile" in workflow  # the verbosity flag is free to change; the worker split is not
    for job, artifact in (("test", "coverage-data-windows"), ("test-linux", "coverage-data-linux")):
        steps = jobs[job]["steps"]
        step = next(step for step in steps if step.get("name") == "Run tests with coverage")
        assert "--cov=harness" in step["run"], job
        whole_step = yaml.safe_dump(step)  # the command and its env: sysmon cannot measure branches on 3.12
        assert "-n auto" not in whole_step and "COVERAGE_CORE" not in whole_step, job
        upload = next(step for step in steps if step.get("name") == "Upload coverage data")
        assert upload["with"]["name"] == artifact and upload["with"]["path"] == ".coverage", job
        assert upload["with"]["include-hidden-files"] is True, job
    assert "--cov-report=xml" not in workflow
    combine = next(step for step in jobs["coverage"]["steps"] if step.get("name") == "Combine coverage and apply the floor")
    assert "coverage combine" in combine["run"] and "xml_report(outfile='coverage.xml')" in combine["run"]
    # The XML names the measured packages as its sources, as pytest-cov's did, so SonarCloud resolves its files.
    assert "source=['harness', 'harness_modules', 'sdk', 'scripts', 'macrunner']" in combine["run"]
    assert workflow.index("xml_report(outfile='coverage.xml')") < workflow.index("SonarSource/sonarqube-scan-action")
    assert "pytest-cov" in workflow
    assert "pytest-xdist" in workflow
    assert "NUMBER_OF_PROCESSORS" in workflow
    assert "jobs:\n  test:" in workflow
    sonar = jobs["sonar"]
    assert sonar["needs"] == "coverage"
    sonar_text = yaml.safe_dump(sonar)
    assert "actions/download-artifact" in sonar_text
    assert "fetch-depth: 0" in sonar_text
    # A failing quality gate blocks on main as it does on pull requests, so it stops the deploy (#728).
    assert "continue-on-error" not in sonar
    assert "runs-on: [self-hosted" not in workflow
    # The Windows suite, its Python 3.13 run and the analysis stay on windows-latest. Ubuntu runs the bookkeeping
    # jobs (`changes`, `gate`, #726), the static checks (`lint`, #729), the Hub's contract suite (`hub-contract`,
    # #736), the review tooling's and installers' Linux tests (`review-linux`, #806; `unix-installers`, #825), the Linux
    # deployment tests (`deploy-linux`, #812), and the Linux suite with the coverage combination (`test-linux`,
    # `coverage`, #973). Only the two suite runs write coverage. `gate-guard` (#727) calls the shared guard, whose
    # diff check runs on the called workflow's ubuntu-latest default, so it has no runs-on of its own.
    for job in ("test", "test-py313", "sonar", "openapi", "ui-tests"):
        assert jobs[job]["runs-on"] == "windows-latest", job
    ubuntu = {name for name, job in jobs.items() if job.get("runs-on") == "ubuntu-latest"}
    assert ubuntu == {"changes", "gate", "lint", "hub-contract", "review-linux", "deploy-linux", "unix-installers",
                      "test-linux", "coverage"}
    for job in ("hub-contract", "review-linux", "deploy-linux", "unix-installers"):
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
