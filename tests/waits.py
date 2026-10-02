"""Deadlines for tests that wait on a session, scaled for slow runners (issue #312).

A wait returns as soon as its condition holds, so a generous deadline costs nothing on a fast machine. The deadline
only decides how long a broken run takes to fail. Hosted CI runners and coverage runs are several times slower than
a dev box, so the deadline grows there. Set HARNESS_TEST_TIMEOUT_SCALE to override the factor.
"""

from __future__ import annotations

import os
import sys


def _coverage_active() -> bool:
    coverage = sys.modules.get("coverage")
    return bool(coverage and coverage.Coverage.current())


def timeout_scale() -> float:
    override = os.environ.get("HARNESS_TEST_TIMEOUT_SCALE")
    if override:
        return float(override)
    return 4.0 if os.environ.get("CI") or _coverage_active() else 1.0


def scaled(seconds: float) -> float:
    """The deadline for a wait that takes well under `seconds` on an idle dev box."""
    return seconds * timeout_scale()
