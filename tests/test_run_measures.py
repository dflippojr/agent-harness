"""Shared per-run measures (bakeoff/run_measures.py, #942): what shell commands read and write, for both drivers.

Each case goes through both drivers' real entry points: Pi's JSON transcript parser (a `bash` call) and the native
session's events (a `run_shell` call), which both feed `file_access`."""

from __future__ import annotations

import json

import pytest

from bakeoff import pi_driver
from bakeoff.run_measures import native_measures

EXISTING = {"calc.py", "notes.md", "util.py", "tests/test_calc.py"}
USAGE = {"input": 10, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 11,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def _pi(command: str, existing: set[str] = EXISTING) -> dict:
    """One Pi turn that runs `command` with its bash tool, through pi_driver.parse_transcript."""
    call = {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": command}}
    msg = {"role": "assistant", "content": [call], "api": "openai-completions", "provider": "canary", "model": "m",
           "usage": USAGE, "stopReason": "toolUse", "timestamp": 1}
    records = [{"type": "turn_start"}, {"type": "message_end", "message": msg},
               {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash", "args": {"command": command}},
               {"type": "turn_end", "message": msg, "toolResults": []}]
    m = pi_driver.parse_transcript("".join(json.dumps(r) + "\n" for r in records), existing)
    return {"read_before_edit": m["read_before_edit"], "edited_unread": m["edited_unread"]}


def _native(command: str, existing: set[str] = EXISTING) -> dict:
    """One native turn that runs `command` with run_shell, through native_measures (session events)."""
    events = [{"type": "tool_call", "data": {"id": "c1", "name": "run_shell", "args": {"command": command}}}]
    m = native_measures({"totals": {}}, events, existing)
    return {"read_before_edit": m["read_before_edit"], "edited_unread": m["edited_unread"]}


DRIVERS = pytest.mark.parametrize("measure", [_pi, _native], ids=["pi", "native"])


# a subshell's redirects open before its body runs
@DRIVERS
@pytest.mark.parametrize("command", ["(cat calc.py) > calc.py", "(cat calc.py)>calc.py", "(cat calc.py) >> calc.py",
                                     "( head calc.py; grep add calc.py ) > calc.py",
                                     "(cd tests; cat ../calc.py) > calc.py", "((cat calc.py)) > calc.py",
                                     "echo start; (cat calc.py) > calc.py"])
def test_a_subshell_s_trailing_redirect_truncates_before_its_body_reads(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
@pytest.mark.parametrize("command", ["cat calc.py; (echo x) > calc.py", "cat calc.py && (cd tests; echo x) > calc.py",
                                     "(cat calc.py); printf x > calc.py", "(printf x > calc.py) < calc.py",
                                     "(cat calc.py; printf x > calc.py)"])
def test_a_read_that_really_comes_first_still_counts(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


@DRIVERS
def test_a_subshell_s_redirect_resolves_where_the_subshell_was_started(measure):
    existing = {"calc.py", "tests/calc.py"}
    # the cd ends with the subshell: the redirect writes the root calc.py, which was never read
    assert measure("(cd tests; cat calc.py) > calc.py", existing) == {"read_before_edit": False,
                                                                      "edited_unread": ["calc.py"]}
