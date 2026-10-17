"""Safety contracts for the image publication and tower deployment workflow."""

from pathlib import Path

import yaml


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


DOCS_REGEN = ROOT / ".github" / "workflows" / "docs-regen.yml"


def _docs_regen_job():
    return yaml.safe_load(DOCS_REGEN.read_text(encoding="utf-8"))["jobs"]["regenerate"]


def test_docs_regen_workflow_cannot_loop_or_deploy():
    workflow = DOCS_REGEN.read_text(encoding="utf-8")
    # Write permissions are scoped to the one job, never workflow-wide.
    assert yaml.safe_load(workflow)["permissions"] == {"contents": "read"}
    assert workflow.count("contents: write") == 1
    # Default GITHUB_TOKEN only: its pushes and merges start no workflow runs, and an explicit skip marker is a
    # second guard on the commit that lands on main.
    assert "secrets." not in workflow
    assert _docs_regen_job()["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "[skip ci]" in _docs_regen_job()["env"]["COMMIT_SUBJECT"]
    assert "contains(github.event.head_commit.message, '[skip ci]')" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "workflow_run" not in workflow
    # The deploy workflow still reacts only to CI, and only to a push to main
    # (test_publish_and_deploy_require_a_successful_main_push_ci_run), so the dispatched docs-branch run never deploys.
    assert "workflows: [CI]" in WORKFLOW.read_text(encoding="utf-8")


def test_docs_regen_never_pushes_to_main():
    # #738: the regenerated docs reach main only through a PR that passed CI, so a ruleset requiring a PR and the
    # `gate` check on main needs no bypass actor for this workflow.
    job = _docs_regen_job()
    branch = job["env"]["DOCS_BRANCH"]
    assert branch.startswith("bot/") and branch not in ("main", "master")
    steps = {step.get("id") or step.get("name"): step for step in job["steps"]}
    scripts = [step["run"] for step in job["steps"] if "run" in step]
    pushes = [line.strip() for script in scripts for line in script.splitlines() if "git push" in line]
    assert pushes == ['git push --force origin "HEAD:refs/heads/$DOCS_BRANCH"']
    every_line = "\n".join(scripts)
    assert "HEAD:main" not in every_line and "refs/heads/main" not in every_line
    # The only way onto main is merging the PR, after CI on its head commit passed, pinned to that same commit.
    order = [step.get("id") or step.get("name") for step in job["steps"]]
    assert order.index("regen") < order.index("ci") < order.index("Merge the docs PR")
    ci = steps["ci"]["run"]
    assert "gh workflow run ci.yml --ref \"$DOCS_BRANCH\"" in ci
    assert '--commit "$HEAD_SHA"' in ci and "--exit-status" in ci
    merge = " ".join(steps["Merge the docs PR"]["run"].split())
    assert merge.startswith('gh pr merge "$PR_NUMBER" --squash --match-head-commit "$HEAD_SHA"')
    assert steps["ci"]["env"]["HEAD_SHA"] == steps["Merge the docs PR"]["env"]["HEAD_SHA"]
    assert set(job["permissions"]) == {"contents", "pull-requests", "actions"}


def test_ci_can_be_dispatched_for_the_docs_pr():
    # A PR opened with GITHUB_TOKEN starts no pull_request run, so docs-regen dispatches CI on its branch;
    # that run compares every region against the branch's own fragments.
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    triggers = yaml.safe_load(ci_text)[True]
    assert "workflow_dispatch" in triggers
    docs_check = ci_text.split('-eq "workflow_dispatch"', 1)[1].split("} else {", 1)[0]
    assert "python scripts/docs/build.py --check\n" in docs_check


def test_main_ci_only_validates_fragments():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "build.py --check --fragments-only" in ci


def test_publish_and_deploy_require_a_successful_main_push_ci_run():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for name in ("publish-images", "deploy-tower"):
        condition = " ".join(str(jobs[name]["if"]).split())
        for required in (
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.head_branch == 'main'",
            "github.event.workflow_run.event == 'push'",
        ):
            assert required in condition, (name, required)
        # Every term must be ANDed: an `||` would let one of them stand alone.
        assert "||" not in condition, name
