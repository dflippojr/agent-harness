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
    # The default token only reads; writes use the dedicated narrow App.
    assert yaml.safe_load(workflow)["permissions"] == {"contents": "read", "actions": "read", "checks": "read"}
    job = _docs_regen_job()
    assert "permissions" not in job
    assert "[skip ci]" not in job["env"]["COMMIT_SUBJECT"]
    assert "[skip ci]" in job["env"]["MERGE_SUBJECT"]
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
    assert order.index("regen") < order.index("ci") < order.index("guard") < order.index("Merge the docs PR")
    ci = steps["ci"]["run"]
    assert "--event pull_request" in ci
    assert "gh workflow run" not in ci
    assert '--commit "$HEAD_SHA"' in ci and "--exit-status" in ci
    merge = " ".join(steps["Merge the docs PR"]["run"].split())
    assert merge.startswith('gh pr merge "$PR_NUMBER" --squash --match-head-commit "$HEAD_SHA"')
    assert steps["ci"]["env"]["HEAD_SHA"] == steps["Merge the docs PR"]["env"]["HEAD_SHA"]
    assert '--subject "$MERGE_SUBJECT (#$PR_NUMBER)"' in merge


def test_docs_pr_uses_repo_scoped_app_for_writes_and_default_token_for_ci_reads():
    job = _docs_regen_job()
    steps = {step.get("id") or step.get("name"): step for step in job["steps"]}
    for token_id in ("app-token", "merge-token"):
        token = steps[token_id]
        assert token["uses"] == "actions/create-github-app-token@v3"
        assert token["with"] == {
            "app-id": "${{ secrets.DOCS_REGEN_APP_ID }}",
            "private-key": "${{ secrets.DOCS_REGEN_APP_PRIVATE_KEY }}",
            "owner": "${{ github.repository_owner }}",
            "repositories": "${{ github.event.repository.name }}",
            "permission-contents": "write",
            "permission-pull-requests": "write",
        }
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["token"] == "${{ steps.app-token.outputs.token }}"
    assert steps["regen"]["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert job["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert steps["Merge the docs PR"]["env"]["GH_TOKEN"] == "${{ steps.merge-token.outputs.token }}"
    order = [step.get("id") or step.get("name") for step in job["steps"]]
    assert order.index("ci") < order.index("merge-token") < order.index("Merge the docs PR")
    ci_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" not in yaml.safe_load(ci_text)[True]


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


def test_deploy_runs_with_pwsh_on_the_linux_production_runner():
    # #812: the tower runs Ubuntu, so the Windows runner label (agent-harness-tower) is retired.
    deploy = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy-tower"]
    assert deploy["runs-on"] == ["self-hosted", "Linux", "X64", "agent-harness-tower-linux"]
    assert deploy["environment"] == "tower-production"
    shells = [step["shell"] for step in deploy["steps"] if "run" in step]
    assert shells and set(shells) == {"pwsh"}
    assert "deploy-ci.ps1" in deploy["steps"][-1]["run"] and "\\" not in deploy["steps"][-1]["run"]
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs", {})
        for name, job in jobs.items():
            labels = job.get("runs-on", [])
            assert "agent-harness-tower" not in ([labels] if isinstance(labels, str) else labels), (path.name, name)
