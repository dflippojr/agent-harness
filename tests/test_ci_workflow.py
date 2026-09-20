"""Contracts for the pytest CI workflow path filter."""

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

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
    "docs/backlog.yaml",
    "docs/compatibility.md",
    "docs/config-registry.md",
    "docs/flux-fast.md",
    "docs/issue-13-lightning-results.md",
    "docs/smart-approvals.md",
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
    "docs/phase8a-design.md",
    "docs/phase8b-results.md",
    "docs/phase8c-results.md",
)


def _event_filters(workflow: dict) -> dict[str, list[str]]:
    # PyYAML 1.1 treats the GitHub `on:` key as boolean True.
    events = workflow.get("on", workflow[True])
    return {
        event: list(events[event]["paths-ignore"])
        for event in ("push", "pull_request")
    }


def test_ci_skips_only_unread_paths_on_push_and_pull_request():
    text = WORKFLOW.read_text(encoding="utf-8")
    filters = _event_filters(yaml.safe_load(text))
    expected = list(EXPECTED_SKIP_PATHS)

    for event, ignored in filters.items():
        assert ignored == expected, event
        assert "LICENSE" in ignored
        for path in MUST_RUN_PATHS:
            assert path not in ignored, path
        assert "**.md" not in ignored
        assert "docs/**" not in ignored

    assert "test_product_naming.py" in text
    assert "test_marketplace_manifest.py" in text
    assert "test_ci_docs_explain_backend_configuration_and_manual_verification" in text
    assert "branch protection" in text
    assert "ci-cd.yml" in text
    assert "does not publish or deploy" in text


def test_ci_installs_pytest_xdist_as_extra_and_runs_fixed_workers():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pip install -r requirements.txt pytest-xdist" in text
    assert "python -m pytest tests -q -n 8 --dist loadfile" in text
    assert "-n auto" not in text
    assert "runs-on: [self-hosted, Windows, X64, agent-harness-ci]" in text
    assert "python -m venv .venv" in text
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest-xdist" not in requirements
    assert "pytest>=" in requirements
