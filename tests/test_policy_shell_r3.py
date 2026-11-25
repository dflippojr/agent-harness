"""Continuation normalization precedes the closed shell grammar (#1064, Decision 2)."""

import shlex

import pytest

from harness.policy import ALLOW, ASK, DENY, Policy
from tests.test_policy import ALLOW_ALL, SHELL_TOOLS
from tests.test_policy_shell_r2 import ENTRY_POINTS, POSITIONS


PUSH = "git -C sub push origin HEAD"
OPERATOR_FORMS = [
    ("&&", ("printf ready {op} " + PUSH,)),
    ("||", ("false {op} " + PUSH,)),
    *[(op, ("{op}out " + PUSH, "git {op}out -C sub push origin HEAD",
             "git -C sub {op}out push origin HEAD")) for op in (">>", ">|", "&>", "&>>")],
    *[(op, ("{op} " + PUSH, "git {op} -C sub push origin HEAD",
             "git -C sub {op} push origin HEAD")) for op in (">&1", "<&0", "2>&1", "3<&0")],
    ("|&", ("printf ready {op} cat; " + PUSH,)),
    ("<>", ("cat {op}file; " + PUSH,)),
    *[(op, ("cat {op}EOF\ndata\nEOF\n" + PUSH,)) for op in ("<<", "<<-")],
    ("<<<", ("cat {op}data; " + PUSH,)),
    *[(op, ("case value in value) printf ready {op} esac; " + PUSH,)) for op in (";;", ";&", ";;&")],
    ("((", ("{op}1+1)); " + PUSH,)),
    ("))", ("((1+1{op}; " + PUSH,)),
]


def _operator_cases():
    for operator, forms in OPERATOR_FORMS:
        # Each internal boundary separately, then all boundaries together.
        placements = {(cut,) for cut in range(1, len(operator))}
        placements.add(tuple(range(1, len(operator))))
        for placement in sorted(placements):
            for repeats in (1, 2):
                spelling = "".join(("\\\n" * repeats if i in placement else "") + char
                                   for i, char in enumerate(operator))
                for form_index, form in enumerate(forms):
                    label = f"{operator}-form{form_index}-cuts{placement}-repeats{repeats}"
                    yield label, form.format(op=spelling)


CONTINUED_OPERATORS = list(_operator_cases())


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("label,source", CONTINUED_OPERATORS, ids=[row[0] for row in CONTINUED_OPERATORS])
def test_joined_operator_grid_preserves_resolvable_repo_push_denial(tool, entry, position, label, source):
    command = position.format(cmd=source)
    command = entry.format(cmd=command, quoted=shlex.quote(command))
    decision = Policy(repo=True).decide(tool, {"command": command})
    assert decision.action == DENY, (label, command, decision)
    assert not decision.smart_eligible


@pytest.mark.parametrize("rules", [[], ALLOW_ALL], ids=["default", "project-allow"])
@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("label,source", CONTINUED_OPERATORS, ids=[row[0] for row in CONTINUED_OPERATORS])
def test_joining_retains_whole_command_opaque_floor(rules, tool, label, source):
    command = source.replace(PUSH, "printf ready").replace("push origin HEAD", "status")
    decision = Policy(rules).decide(tool, {"command": command})
    assert decision.action == ASK, (label, command, decision)
    assert not decision.smart_eligible


@pytest.mark.parametrize("rules", [[], ALLOW_ALL], ids=["default", "project-allow"])
@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("module", ["todo", "project_tools", "project_tools.cli"])
@pytest.mark.parametrize("startup", ["-m {module}", "-Im{module}", "-W ignore -m {module}", "-X dev -m {module}"])
def test_project_modules_stay_opaque(rules, tool, entry, module, startup):
    source = "python " + startup.format(module=module) + " list"
    command = entry.format(cmd=source, quoted=shlex.quote(source))
    decision = Policy(rules).decide(tool, {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", SHELL_TOOLS)
def test_unchanged_canary_command_keeps_specific_approval_reason(tool):
    command = "cd /workspace && rm -f /tmp/todo.json && python -m todo list"
    decision = Policy().decide(tool, {"command": command})
    assert decision.action == ASK
    assert decision.reason == "deletes files outside the scratch area"
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
@pytest.mark.parametrize("source", [
    "printf '%s' '>\\\n|'", "printf '%s' '$\\\n(name)'", "printf '%s' '`\\\nname`'",
    "printf '%s' 'literal\\\ntext'", "git -C '>\\\n|' status",
])
def test_single_quoted_continuations_remain_literal(tool, source):
    assert Policy().decide(tool, {"command": source}).action == ALLOW


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("source,expected", [
    ("printf '%s' " + "\\" * 2 + "\n" + PUSH, DENY),
    ("printf '%s' " + "\\" * 3 + "\n" + PUSH, ASK),
    ("printf '%s' " + "\\" * 4 + "\n" + PUSH, DENY),
    ("# comment ' \\\n" + PUSH, DENY),
    ("# comment \" \\\n" + PUSH, DENY),
    ("git -C \\>\\\n\\| push origin HEAD", DENY),
    ('git -C ">\\\n|" push origin HEAD', DENY),
])
def test_normalization_keeps_quote_escape_and_comment_provenance(tool, source, expected):
    assert Policy(repo=True).decide(tool, {"command": source}).action == expected
