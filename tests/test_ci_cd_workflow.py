"""Safety contracts for the image publication and tower deployment workflow."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-cd.yml"


def test_actions_cache_export_cannot_fail_image_publication():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    cache_exports = [
        line.strip() for line in workflow.splitlines() if line.strip().startswith("cache-to:")
    ]
    assert len(cache_exports) == 2
    assert all("type=gha" in line and "ignore-error=true" in line for line in cache_exports)


def test_jobs_keep_only_required_package_permissions():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    publish = workflow.split("  publish-images:", 1)[1].split("  deploy-tower:", 1)[0]
    deploy = workflow.split("  deploy-tower:", 1)[1]
    assert "packages: write" in publish
    assert "packages: read" in deploy
