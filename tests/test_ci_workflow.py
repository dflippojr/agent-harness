"""Contracts for the pytest CI workflow path filter."""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PULL_REQUEST_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
TRUSTED_GATE_GUARD = ROOT / ".github" / "workflows" / "gate-guard-target.yml"
REVIEW_WORKFLOW = ROOT / ".github" / "workflows" / "review.yml"
EARLY_GATE_GUARD_USES = re.compile(r"dflippojr/\.github/\.github/workflows/gate-guard\.yml@([0-9a-f]{40})")
TRUSTED_GATE_GUARD_USES = re.compile(r"dflippojr/\.github/\.github/workflows/gate-guard-target\.yml@([0-9a-f]{40})")

# Files pytest currently reads; skipping them would hide real failures.
MUST_RUN_PATHS = (
    "README.md",
    "docs/web.md",
    "docs/control-center.md",
    "docs/INSTALL.md",
    "docs/admin-api.md",
    "docs/app-api.md",
    "docs/mac-client.md",
    "docs/service-profile.md",
    "docs/marketplace-design.md",
    "docs/marketplace-threat-model.md",
    "docs/marketplace-manifest.schema.json",
    "docs/marketplace-manifest.ordinary.example.json",
    "docs/marketplace-manifest.elevated.example.json",
    "docs/CI-CD.md",
)

# Allow-list of unread files. Prefer extra runs over skipping a newly read path.
EXPECTED_SKIP_PATHS = (
    "LICENSE",
    "third_party/Real-ESRGAN.LICENSE",
    "docs/phase0-results.md",
    "docs/phase1-results.md",
    "docs/phase2-results.md",
    "docs/phase3-results.md",
    "docs/phase4-results.md",
    "docs/phase5-results.md",
    "docs/phase6a-hermes-study.md",
    "docs/phase6b-results.md",
    "docs/phase6c-results.md",
    "docs/phase6d-results.md",
    "docs/phase6e-results.md",
    "docs/phase7a-results.md",
    "docs/phase7b-results.md",
    "docs/phase7d-results.md",
    "docs/phase7e-results.md",
    "docs/phase8b-results.md",
    "docs/phase8c-results.md",
)


def _skip_list(workflow: dict) -> list[str]:
    step = next(s for s in workflow["jobs"]["changes"]["steps"] if s.get("id") == "filter")
    return [line for line in step["env"]["CI_SKIP_UNREAD_PATHS"].splitlines() if line]


def test_ci_skips_only_unread_paths_on_push_and_pull_request():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    # PyYAML 1.1 treats the GitHub `on:` key as boolean True.
    events = workflow.get("on", workflow[True])
    # The workflow always runs so `gate` always reports (#726); the skip moved into the `changes` job.
    for event in ("push", "pull_request"):
        assert "paths-ignore" not in events[event], event
        assert "paths" not in events[event], event
    ignored = _skip_list(workflow)
    assert ignored == list(EXPECTED_SKIP_PATHS)
    assert "LICENSE" in ignored
    for path in MUST_RUN_PATHS:
        assert path not in ignored, path
    assert "**.md" not in ignored
    assert "docs/**" not in ignored
    # Entries are matched exactly by scripts/ci_gate.py; a glob would silently never match.
    assert not any(ch in path for path in ignored for ch in "*?[!")

    assert "test_product_naming.py" in text
    assert "test_marketplace_manifest.py" in text
    assert "test_ci_docs_explain_backend_configuration_and_manual_verification" in text
    assert "ci-cd.yml" in text


def test_ci_gate_guard_job_is_an_early_signal_pinned_by_sha_and_gate_needs_it():
    # A pull request supplies this job and can replace it, so it is not the judge: gate-guard-target.yml is, and the
    # ruleset requires it (dflippojr/agent-harness#727). This job still fails a pull request early, as the spec asks.
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    guard = jobs["gate-guard"]
    match = EARLY_GATE_GUARD_USES.fullmatch(guard["uses"])
    assert match, guard["uses"]
    assert guard["with"]["guard-ref"] == match.group(1)
    # Only the owner's gate-change-approved label counts (dflippojr/.github CONTRIBUTING.md).
    assert guard["with"]["require-owner-label-actor"] is True
    assert guard["if"] == "github.event_name == 'pull_request'"
    # The called workflow reads issues too, and a called workflow cannot widen the caller's permissions.
    assert guard["permissions"]["issues"] == "read"
    assert "gate-guard" in jobs["gate"]["needs"]


def test_hub_contract_runs_the_pinned_hubs_daemon_cases_against_this_checkout():
    # The Hub's contract suite judges each daemon change (#736). A deliberate API break bumps the pin (AGENTS.md).
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job = jobs["hub-contract"]
    assert "hub-contract" in jobs["gate"]["needs"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 10
    checkouts = [step["with"] for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts[0]["path"] == "daemon" and "repository" not in checkouts[0]
    hub = checkouts[1]
    assert hub["repository"] == "dflippojr/agent-harness-hub"
    assert re.fullmatch(r"[0-9a-f]{40}", hub["ref"]), hub["ref"]
    assert all(checkout["persist-credentials"] is False for checkout in checkouts)
    install = next(step["run"] for step in job["steps"] if step.get("name") == "Install the daemon and the Hub")
    assert "python -m pip install -r daemon/requirements.txt" in install
    assert "--require-hashes -r hub/requirements-e2e.lock" in install
    suite = job["steps"][-1]
    assert suite["working-directory"] == "hub"
    assert suite["env"]["HUB_E2E_DAEMON_SRC"] == "${{ github.workspace }}/daemon"
    assert suite["env"]["HUB_E2E_DAEMON_PYTHON"] == "${{ steps.python.outputs.python-path }}"
    assert "-m pytest tests/contract -m e2e" in suite["run"]


def test_ci_reruns_when_an_owner_label_changes_so_gate_reevaluates_the_early_guard():
    # The early gate-guard job reads labels when it runs, and `gate` needs it. Without labeled/unlabeled here, applying
    # gate-change-approved leaves the failed gate in place until someone reruns CI by hand (#727).
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML 1.1 treats the GitHub `on:` key as boolean True.
    types = workflow.get("on", workflow[True])["pull_request"]["types"]
    assert {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= set(types)


def test_trusted_gate_guard_runs_from_main_pinned_by_sha_and_checks_out_nothing():
    text = TRUSTED_GATE_GUARD.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    # PyYAML 1.1 treats the GitHub `on:` key as boolean True.
    events = workflow.get("on", workflow[True])
    # pull_request_target runs the file as it is on main, so a pull request cannot supply its own judge.
    assert set(events) == {"pull_request_target"}
    assert events["pull_request_target"]["types"] == [
        "opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "closed"]
    assert workflow["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read"}
    assert workflow["concurrency"]["group"] == "gate-guard-target-${{ github.event.pull_request.head.sha }}"
    # The job id is the required check's name: gate-guard-target / gate-guard.
    assert list(workflow["jobs"]) == ["gate-guard-target"]
    target = workflow["jobs"]["gate-guard-target"]
    match = TRUSTED_GATE_GUARD_USES.fullmatch(target["uses"])
    assert match, target["uses"]
    # Pinned by commit, not branch, and guard-ref names the same commit.
    assert target["with"]["guard-ref"] == match.group(1)
    # The caller runs no pull request code and reads no secrets: no steps, no checkout, no secrets.
    assert "steps" not in target and "secrets" not in target
    assert "actions/checkout" not in text
    assert "pull_request:" not in text and "workflow_dispatch" not in text


def test_review_workflow_grants_the_issues_read_that_review_v3_needs():
    # A called workflow cannot hold more than its caller grants; review-v3 reads the linked issues (the spec).
    permissions = yaml.safe_load(REVIEW_WORKFLOW.read_text(encoding="utf-8"))["permissions"]
    assert permissions["issues"] == "read"


def test_pull_request_template_carries_the_shared_proof_table():
    template = PULL_REQUEST_TEMPLATE.read_text(encoding="utf-8")
    assert "## Proof" in template
    assert "| Acceptance criterion | How verified" in template
    assert "### Not verified" in template
    assert "### Gate changes" in template
