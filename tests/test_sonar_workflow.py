"""Contracts for SonarCloud coverage reporting and the quality-gate wait."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sonar.yml"
PROPERTIES = ROOT / "sonar-project.properties"


def test_sonar_job_produces_coverage_xml_before_the_scan():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    coverage_at = workflow.index("--cov-report=xml:coverage.xml")
    scan_at = workflow.index("SonarSource/sonarqube-scan-action")
    assert coverage_at < scan_at
    assert "pytest-cov" in workflow
    assert "pytest-xdist" in workflow
    assert "python -m pytest tests -q -n 4 --dist loadfile" in workflow
    coverage_step = workflow[workflow.index("Run tests with coverage") : workflow.index("Scan with SonarCloud")]
    assert "-n auto" not in coverage_step
    assert "COVERAGE_CORE" not in coverage_step
    assert "NUMBER_OF_PROCESSORS" in workflow
    assert "jobs:\n  sonar:" in workflow
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
