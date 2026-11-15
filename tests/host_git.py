"""Tests that run host git on a workspace, which Windows refuses for now (#981, step 2 of #973).

`harness.projects.require_host_git` refuses every host git operation on a workspace while `_REFUSE_HOST_GIT` is set
(on Windows, until the handle-relative state copy of #973 step 3 lands). A test marked `needs_host_git` exercises such
an operation, so it is skipped there; CI's Linux jobs (`test-linux`, #976) run it, and their coverage counts toward
the combined floor. `tests/test_windows_git_refusal.py` asserts the refusal itself on every OS.

The skip is keyed on the same flag as the refusal, so it ends when the flag does; #973 step 3 then removes the marker.
"""
from __future__ import annotations

import pytest

from harness import projects

needs_host_git = pytest.mark.skipif(
    projects._REFUSE_HOST_GIT,
    reason="host git operations on workspaces refuse on Windows until #973 step 3; the Linux jobs run this test")
