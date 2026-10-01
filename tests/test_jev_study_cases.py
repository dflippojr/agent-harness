"""Seed cases for the #160 Jev shadow-comparison study stay inside ASK-tier review scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.policy import ASK, Policy
from harness.smart_approvals import _secretish, assess_eligibility

BASH = "Bash"
DATA = json.loads((Path(__file__).parent / "fixtures" / "jev_study_cases.json").read_text(encoding="utf-8"))
CASES = DATA["cases"]
CONTROLS = DATA["out_of_scope_controls"]


def _assess(command: str):
    decision = Policy().decide(BASH, {"command": command})
    return decision, assess_eligibility(BASH, {"command": command}, decision, repo=False)


def test_case_ids_unique_and_labels_known():
    ids = [c["id"] for c in CASES] + [c["id"] for c in CONTROLS]
    assert len(ids) == len(set(ids))
    assert {c["label"] for c in CASES} == set(DATA["labels"])
    assert {"safe", "injection", "effect-name", "comment-quote"} == {c["category"] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_reaches_the_reviewer(case):
    # A case the static gate rejects would never be sent to any reviewer, so it cannot be in the study set.
    decision, el = _assess(case["command"])
    assert decision.action == ASK
    assert el.ok, el.reason


@pytest.mark.parametrize("case", CONTROLS, ids=lambda c: c["id"])
def test_controls_are_rejected_before_review(case):
    _decision, el = _assess(case["command"])
    assert not el.ok
    assert el.reason == case["gate_reason"]


def test_cases_carry_no_secrets_or_absolute_paths():
    for case in CASES + CONTROLS:
        command = case["command"]
        assert not _secretish(command), case["id"]
        assert ":\\" not in command and "/home/" not in command and "/Users/" not in command, case["id"]
