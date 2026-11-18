"""Issue #891: the README names the review caller tag that docs/CI-CD.md documents."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
CALLER_PIN = re.compile(r"dflippojr/agent-harness/\.github/workflows/review\.yml@(review-v\d+)")
REVIEW_TAG = re.compile(r"\breview-v\d+\b")


def test_readme_review_tags_match_the_caller_tag_in_ci_cd_doc():
    ci_cd = (ROOT / "docs" / "CI-CD.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    (documented,) = set(CALLER_PIN.findall(ci_cd))  # the caller example in docs/CI-CD.md
    assert set(REVIEW_TAG.findall(readme)) <= {documented}
