"""Shell syntax must retain its meaning through policy normalization (#1064, R1)."""

import shlex

import pytest

from harness.policy import ALLOW, ASK, DENY, Policy, _delete_outside_scratch
from tests.test_policy import SHELL_SPELLINGS, SHELL_TOOLS


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("spelling,template", SHELL_SPELLINGS, ids=[row[0] for row in SHELL_SPELLINGS])
@pytest.mark.parametrize("operator", [";", "|", "&", "(", ")", "{", "}", ">", "<", ">>", "2>&1"])
@pytest.mark.parametrize("quoting", ["single", "double", "escape"])
def test_literal_operators_never_hide_delete_targets_or_git_options(tool, spelling, template, operator, quoting):
    literal = {"single": shlex.quote(operator), "double": '"' + operator + '"',
               "escape": "".join("\\" + c for c in operator)}[quoting]
    for plain, expected in ((f"rm -rf scratch {literal} src", ASK),
                            (f"find . -name {literal} -delete", ASK),
                            (f"git -C {literal} push origin HEAD", DENY)):
        command = template.format(cmd=plain, quoted=shlex.quote(plain), flags=plain)
        assert Policy(repo=True).decide(tool, {"command": command}).action == expected, (spelling, command)


CONTINUATION_COMMANDS = [
    ("r\\\nm -rf src", ASK),
    ("rm -r\\\nf src", ASK),
    ("git pu\\\nsh origin HEAD", DENY),
    ("git reset --ha\\\nrd HEAD", ASK),
    ("git clean --fo\\\nrce -d", ASK),
    ("gi\\\nt -C sub push origin HEAD", DENY),
]


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("spelling,template", SHELL_SPELLINGS, ids=[row[0] for row in SHELL_SPELLINGS])
@pytest.mark.parametrize("plain,expected", CONTINUATION_COMMANDS)
def test_line_continuations_preserve_hazards_through_wrappers(tool, spelling, template, plain, expected):
    command = template.format(cmd=plain, quoted=shlex.quote(plain), flags=plain)
    assert Policy(repo=True).decide(tool, {"command": command}).action == expected, (spelling, command)


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("spelling,template", SHELL_SPELLINGS, ids=[row[0] for row in SHELL_SPELLINGS])
@pytest.mark.parametrize("plain", ["git reset --har HEAD", "git reset --h HEAD", "git clean --for -d",
                                   "git clean --fo -d", "git clean --f -d"])
def test_abbreviated_git_destructive_flags_ask(tool, spelling, template, plain):
    command = template.format(cmd=plain, quoted=shlex.quote(plain), flags=plain)
    assert Policy().decide(tool, {"command": command}).action == ASK, (spelling, command)


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("prefix", ["", "env ", "command -- ", "timeout 10 "])
@pytest.mark.parametrize("options", ["-W ignore -c", "-X dev -c", "-Ic", "-IW ignore -c", "-IX dev -c",
                                     "-Wignore -c", "-Xdev -c", "--unknown-option value -c"])
def test_interpreter_options_cannot_hide_inline_execution(tool, prefix, options):
    command = prefix + "/usr/bin/python3 " + options + " 'print(1)'"
    decision = Policy().decide(tool, {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
@pytest.mark.parametrize("command", [
    "rg '$(NAME)' Makefile", "echo '$(hello)'", "printf '%s' '`hello`'", 'echo "cost $\'text\'"',
    r"echo \$\(hello\)", r"printf '%s' \`hello\`", 'echo "\\$(hello)"',
    "echo 'r\\\nm'", "git -C 'r\\\nm' status", "python -m pytest -c pytest.ini",
    "python -W ignore -m pytest -c pytest.ini", "python -X dev -m pytest -c pytest.ini",
    "python -IW ignore -m pytest -c pytest.ini",
])
def test_literal_substitution_text_and_module_arguments_remain_allowed(tool, command):
    assert Policy().decide(tool, {"command": command}).action == ALLOW


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("command", ["echo $(hello)", 'echo "$(hello)"', "echo `hello`", 'echo "`hello`"'])
def test_executed_substitutions_remain_human_only(tool, command):
    decision = Policy().decide(tool, {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("command", ["rm -rf scratch 2 >/tmp/out", "rm -rf scratch '2'>/tmp/out",
                                     "find . -exec rm -rf scratch '>' src +", "rm -rf scratch { src",
                                     "rm -rf scratch } src"])
def test_redirection_stripping_preserves_literal_arguments(command):
    assert Policy().decide("exec_command", {"command": command}).action == ASK


@pytest.mark.parametrize("command", ["python -c 'print(1)'", "echo $(hello)", "git --unknown x"])
def test_opaque_execution_is_not_reported_as_a_file_deletion(command):
    assert not _delete_outside_scratch(command)
