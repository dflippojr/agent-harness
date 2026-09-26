"""Issue #28: curated catalog manifests stay schema-strict and secret-free."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from harness.apps import SCOPES
from harness.config import MODULE_NAMES

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SCHEMA_PATH = DOCS / "marketplace-manifest.schema.json"
ORDINARY = DOCS / "marketplace-manifest.ordinary.example.json"
ELEVATED = DOCS / "marketplace-manifest.elevated.example.json"
DESIGN = DOCS / "marketplace-design.md"
THREAT = DOCS / "marketplace-threat-model.md"

REQUIRED_ABUSE_CASES = (
    "Malicious publisher",
    "Compromised publisher",
    "Release / source mismatch",
    "Artifact compromise",
    "Catalog compromise",
    "Dependency confusion",
    "Signature-key theft",
    "Typosquatting",
    "Origin takeover",
    "Scope creep",
    "App-token theft",
    "Approval spoofing",
    "Cross-app / cross-session data access",
    "Prompt / context exfiltration",
    "Provider-credential capture",
    "Undisclosed telemetry",
    "Unsafe auto-update",
    "Review bypass",
    "Reviewer compromise",
    "Abandoned software",
)


def canonical_app_bytes(app: dict) -> bytes:
    return json.dumps(app, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema() -> dict:
    return load_json(SCHEMA_PATH)


@pytest.fixture(scope="module")
def validator(schema) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("path", [ORDINARY, ELEVATED], ids=["ordinary", "elevated"])
def test_example_manifests_validate(validator, path):
    document = load_json(path)
    validator.validate(document)
    digest = hashlib.sha256(canonical_app_bytes(document["app"])).hexdigest()
    assert document["review"]["publisher_manifest_sha256"] == digest
    artifacts = {item["filename"]: item["sha256"] for item in document["app"]["artifacts"]}
    recorded = {item["filename"]: item["sha256"] for item in document["review"]["artifact_digests"]}
    assert artifacts == recorded
    assert document["app"]["provider_auth"]["never_handles_provider_credentials"] is True
    assert "admin" not in {item["scope"] for item in document["app"]["scopes"]}


def test_unknown_fields_fail_closed(validator):
    document = load_json(ORDINARY)
    document["unexpected_top_level"] = True
    with pytest.raises(Exception, match="additionalProperties|unexpected_top_level"):
        validator.validate(document)

    nested = load_json(ORDINARY)
    nested["app"]["telemetry"]["extra"] = "nope"
    with pytest.raises(Exception, match="additionalProperties|extra"):
        validator.validate(nested)


def test_admin_is_not_an_app_scope(validator):
    document = load_json(ORDINARY)
    document["app"]["scopes"] = [{
        "scope": "admin",
        "justification": "Owner API access is never granted to catalog apps under any review outcome.",
        "risk_tier": "elevated",
    }]
    with pytest.raises(Exception, match="admin"):
        validator.validate(document)


def test_provider_credentials_cannot_be_handled(validator):
    document = load_json(ORDINARY)
    document["app"]["provider_auth"]["never_handles_provider_credentials"] = False
    with pytest.raises(ValidationError, match="True was expected"):
        validator.validate(document)


def test_elevated_scope_requires_elevated_tier(validator):
    document = load_json(ELEVATED)
    for item in document["app"]["scopes"]:
        if item["scope"] == "sessions:all":
            item["risk_tier"] = "standard"
    with pytest.raises(ValidationError, match="'elevated' was expected"):
        validator.validate(document)


def test_examples_and_schema_contain_no_secrets():
    blobs = [
        SCHEMA_PATH.read_text(encoding="utf-8"),
        ORDINARY.read_text(encoding="utf-8"),
        ELEVATED.read_text(encoding="utf-8"),
        DESIGN.read_text(encoding="utf-8"),
        THREAT.read_text(encoding="utf-8"),
    ]
    joined = "\n".join(blobs)
    # The design mentions prefixes as names of things to reject; require they only appear as documented markers.
    assert "ha-secret" not in joined
    assert "sk-ant-api" not in joined.lower()
    for path in (SCHEMA_PATH, ORDINARY, ELEVATED):
        text = path.read_text(encoding="utf-8")
        for marker in ("BEGIN PRIVATE KEY", "Bearer ha-", "Bearer ho-", "hp-live"):
            assert marker not in text


def test_design_covers_every_current_app_scope_and_modules():
    design = DESIGN.read_text(encoding="utf-8")
    for scope in SCOPES:
        assert f"`{scope}`" in design
    assert "`admin`" in design
    assert "prohibited" in design.lower()
    for module in MODULE_NAMES:
        assert f"`{module}`" in design
    for backend in ("local", "claude", "codex", "cursor"):
        assert backend in design
    assert "GET /api/v1" in design
    assert "/api/admin/v1" in design
    assert "normalize_origin" in design
    assert "provider_policy" in design
    assert "manifest_schema_version" in design
    assert "additionalProperties" in design


def test_threat_model_covers_required_abuse_cases():
    threat = THREAT.read_text(encoding="utf-8")
    for title in REQUIRED_ABUSE_CASES:
        assert title in threat, title
    for heading in ("Prevention", "Detection", "Response", "Residual"):
        assert heading in threat
    assert "cannot technically enforce" in threat.lower() or "cannot prove" in threat.lower()
    assert "outside the daemon sandbox" in threat.lower()


def test_elevated_example_uses_two_reviewers_and_reconsent_scopes():
    document = load_json(ELEVATED)
    scopes = {item["scope"]: item["risk_tier"] for item in document["app"]["scopes"]}
    assert scopes["sessions:all"] == scopes["approvals"] == scopes["remote_control"] == "elevated"
    assert len(document["review"]["reviewer_ids"]) >= 2
    assert document["app"]["browser_origins"] == ["https://ops.example.com"]
    assert document["review"]["permission_diff"]["kind"] == "expansion"


def test_mutating_a_published_record_changes_the_manifest_digest():
    document = load_json(ORDINARY)
    original = document["review"]["publisher_manifest_sha256"]
    mutated = copy.deepcopy(document["app"])
    mutated["summary"] += " extra"
    assert hashlib.sha256(canonical_app_bytes(mutated)).hexdigest() != original
