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
    # The published images are public (this repository is public), so the tower pulls them anonymously.
    # A GITHUB_TOKEN login on the tower was rejected ("denied") and is not needed.
    assert "packages:" not in deploy
    assert "docker login" not in deploy


def test_docs_regen_workflow_cannot_loop_or_deploy():
    workflow = (ROOT / ".github" / "workflows" / "docs-regen.yml").read_text(encoding="utf-8")
    # contents: write is scoped to the one job, never workflow-wide.
    assert workflow.split("jobs:", 1)[0].count("contents: write") == 0
    assert workflow.count("contents: write") == 1
    # Default GITHUB_TOKEN only (its pushes start no workflow runs), and an explicit skip marker as a second guard.
    assert "secrets." not in workflow and "token:" not in workflow
    assert "[skip ci]" in workflow.split("git commit", 1)[1].splitlines()[0]
    assert "contains(github.event.head_commit.message, '[skip ci]')" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "workflow_run" not in workflow
    # The deploy workflow still reacts only to CI, which never runs for the bot commit.
    assert "workflows: [CI]" in WORKFLOW.read_text(encoding="utf-8")


def test_main_ci_only_validates_fragments():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "build.py --check --fragments-only" in ci
