"""Contracts for SonarCloud coverage reporting and the quality-gate wait."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PROPERTIES = ROOT / "sonar-project.properties"


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
