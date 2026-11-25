"""Closed shell grammar: unresolved syntax never inherits an allow (#1064, Decision 1)."""

import shlex

import pytest

from harness.policy import ALLOW, ASK, DENY, Policy
from tests.test_policy import ALLOW_ALL, SHELL_TOOLS


# Every R1/R2 finding has a row. R1's harmless literal-substitution finding also
# has positive controls below: literal text is modeled data, not opaque syntax.
CASES = [
    ("r1-quoted-redirect", "rm -rf scratch '>' src", ASK),
    ("r1-quoted-separator", "rm -rf scratch ';' src", ASK),
    ("r1-quoted-find", "find . -name ';' -delete", ASK),
    ("r1-quoted-git-directory", "git -C ';' push origin HEAD", DENY),
    ("r1-continued-command", "r\\\nm -rf src", ASK),
    ("r1-continued-push", "git pu\\\nsh origin HEAD", DENY),
    ("r1-abbreviated-reset", "git reset --har HEAD", ASK),
    ("r1-abbreviated-clean", "git clean --for -d", ASK),
    ("r1-interpreter-arity", "env python3 -W ignore -c 'print(1)'", ASK),
    ("r1-interpreter-bundle", "python3 -Ic 'print(1)'", ASK),
    ("r1-active-substitution", 'echo "$(hello)"', ASK),
    ("r2-continued-substitution", 'printf %s "$\\\n(rm src)"', ASK),
    ("r2-repeated-continuation", 'printf %s "prefix$\\\n\\\n(rm src)suffix"', ASK),
    ("r2-unquoted-continuation", 'echo $\\\n(rm src)', ASK),
    ("r2-git-push-alias", "git -c alias.p=push p origin HEAD", DENY),
    ("r2-attached-git-push-alias", "git -calias.p=push p origin HEAD", DENY),
    ("r2-git-shell-alias", "git -c 'alias.wipe=!rm -rf src' wipe", ASK),
    ("r2-git-exec-path", "git --exec-path=helpers wipe", ASK),
    ("r2-git-ssh-command", "git -c 'core.sshCommand=rm -f src #' fetch fixture", ASK),
    ("r2-git-helper", "git -c credential.helper=custom status", ASK),
    ("r2-git-config-env", "git --config-env=alias.p=GIT_ALIAS p", ASK),
    ("r2-env-helper", "env GIT_EXEC_PATH=helpers git wipe", ASK),
    ("r2-assigned-helper", "GIT_SSH_COMMAND=helper git status", ASK),
    ("r2-python-stdin", "python3 --", ASK),
    ("r2-python-stdin-marker", "python3 -- -", ASK),
    ("r2-python-flags-stdin", "python3 -O --", ASK),
    ("r2-python-option-stdin", "python3 -X dev -- -", ASK),
    ("r2-piped-python-stdin", "printf '%s' 'print(1)' | python3 --", ASK),
    ("r2-node-stdin", "node --", ASK),
    ("r2-perl-stdin", "perl --", ASK),
    ("r2-ruby-stdin", "ruby -- -", ASK),
    ("r2-clobber-prefix", ">|out git push origin HEAD", DENY),
    ("r2-clobber-infix", "git >|out push origin HEAD", DENY),
    ("r2-clobber-cwd", "git -C sub >|out push origin HEAD", DENY),
    ("unmodeled-case-operator", "echo x ;& cat README.md", ASK),
    ("unmodeled-fallthrough", "echo x ;;& cat README.md", ASK),
    ("unmodeled-pipe-operator", "echo x |& cat README.md", ASK),
    ("unmodeled-read-write", "cat <>file", ASK),
    ("unmodeled-here-string", "cat <<<text", ASK),
    ("unmodeled-arithmetic", "echo $((1+1))", ASK),
    ("unmodeled-variable", 'echo "$FILE"', ASK),
    ("unmodeled-glob", "echo *.py", ASK),
    ("unmodeled-brace", "echo {src,build}", ASK),
    ("unmodeled-tilde", "echo ~/file", ASK),
    ("unmodeled-function", "f(){ echo ok; }; f", ASK),
    ("unmodeled-control", "for x in src; do echo ok; done", ASK),
    ("unmodeled-git-command", "git custom-helper", ASK),
    ("unmodeled-git-option", "git status --unknown", ASK),
    ("unmodeled-git-config", "git -c custom.setting=value status", ASK),
    ("unmodeled-python-module", "python -m unknown_module", ASK),
    ("unmodeled-python-startup", "python -X presite=custom -m pytest", ASK),
    ("unmodeled-python-warning", "python -W error::custom.Warning -m pytest", ASK),
    ("unmodeled-shell-option", "sh --unknown -c 'git status'", ASK),
    ("unmodeled-environment", "env UNMODELED=value git status", ASK),
    ("unmodeled-env-identifier", "env 'BASH_FUNC_f%%=() { echo x; }' bash -c f", ASK),
    ("unmodeled-git-pager", "git --paginate status", ASK),
    ("resolved-alias-chain", "git -c alias.p=q -c alias.q=push p origin HEAD", DENY),
    ("resolved-shell-alias-push", "git -c 'alias.p=!git push' p origin HEAD", DENY),
    ("r2b-canary-cd-rm-todo", "cd /workspace && rm -f /tmp/todo.json && python -m todo list", ASK),
]
POSITIONS = ["{cmd}; echo tail", "echo head; {cmd}; echo tail", "echo head; {cmd}"]
ENTRY_POINTS = ["{cmd}", "command -- sh -c {quoted}", "env LC_ALL=C sh -c {quoted}", "({cmd})"]


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("case,source,expected", CASES, ids=[row[0] for row in CASES])
def test_closed_grammar_grid_never_allows_unresolved_execution(tool, entry, position, case, source, expected):
    command = position.format(cmd=source)
    command = entry.format(cmd=command, quoted=shlex.quote(command))
    decision = Policy(repo=True).decide(tool, {"command": command})
    assert decision.action == expected, (case, command, decision)
    assert not decision.smart_eligible, (case, command, decision)
    if case == "r2b-canary-cd-rm-todo":
        assert decision.reason == "deletes files outside the scratch area"


@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
@pytest.mark.parametrize("command", [
    "git -c color.ui=false status", "git -ccolor.ui=false log --oneline", "python -m pytest -c pytest.ini",
    "python -W ignore -X dev -m pytest -c pytest.ini", "python -- script.py", "node -- script.js",
    "perl -- script.pl", "ruby -- script.rb", "echo literal >|out", "git >|out status",
    "rg '$(NAME)' Makefile", "echo '$(hello)'", "printf '%s' '`hello`'", r"echo \$\(hello\)",
])
def test_explicitly_modeled_benign_forms_stay_allowed(tool, command):
    assert Policy().decide(tool, {"command": command}).action == ALLOW


@pytest.mark.parametrize("command", [
    'printf %s "$\\\n(rm src)"', "git --exec-path=helpers status", "python --", "echo *.py",
    "echo before; git -c credential.helper=helper status; echo after", "git status --unknown",
])
@pytest.mark.parametrize("tool", SHELL_TOOLS)
def test_opaque_floor_cannot_be_overridden_by_project_allow(command, tool):
    decision = Policy(ALLOW_ALL).decide(tool, {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("command", [
    "git push origin HEAD", "git -c alias.p=push p origin HEAD", ">|out git push origin HEAD",
    "git --exec-path=helpers push origin HEAD", "echo *.py; git push origin HEAD",
])
def test_resolvable_push_keeps_repo_deny_even_with_unknown_context(command):
    assert Policy(ALLOW_ALL, repo=True).decide("exec_command", {"command": command}).action == DENY


def test_alias_resolution_is_bounded_and_stays_human_only():
    aliases = " ".join(f"-c alias.p{i}=p{i + 1}" for i in range(25))
    command = f"git {aliases} p0"
    decision = Policy().decide("exec_command", {"command": command})
    assert decision.action == ASK
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("rules", [[], ALLOW_ALL], ids=["default", "project-allow"])
@pytest.mark.parametrize("hazard,reason", [
    ("rm -rf src", "deletes files outside the scratch area"),
    ("cd /workspace && rm -f /tmp/todo.json", "deletes files outside the scratch area"),
    ("git reset --hard HEAD", "discards uncommitted work"),
    ("git clean --for -d", "discards uncommitted work"),
    ("git push origin HEAD", "git push"),
])
@pytest.mark.parametrize("opaque", ["python -m unknown_module", 'echo "$VALUE"',
                                    "git --exec-path=helpers status"])
@pytest.mark.parametrize("before", [False, True], ids=["opaque-after", "opaque-before"])
def test_known_hazard_reason_survives_other_opaque_parts(tool, rules, hazard, reason, opaque, before):
    command = f"{opaque}; {hazard}" if before else f"{hazard}; {opaque}"
    decision = Policy(rules).decide(tool, {"command": command})
    assert decision.action == ASK
    assert decision.reason == reason
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", SHELL_TOOLS)
@pytest.mark.parametrize("rules", [[], ALLOW_ALL], ids=["default", "project-allow"])
def test_network_hazard_reason_survives_opaque_execution(tool, rules):
    decision = Policy(rules).decide(tool, {"command": "python -m unknown_module", "network": True})
    assert decision.action == ASK
    assert decision.reason == "command needs network access"
    assert not decision.smart_eligible


@pytest.mark.parametrize("tool", ["run_shell", "exec_command"])
@pytest.mark.parametrize("command", ["cd /workspace", "python -m pytest -q", "cd /workspace && python -m pytest -q"])
def test_supported_module_and_absolute_cd_are_modeled(tool, command):
    assert Policy().decide(tool, {"command": command}).action == ALLOW


@pytest.mark.parametrize("tool", SHELL_TOOLS)
def test_opaque_without_a_known_hazard_keeps_generic_human_only_reason(tool):
    decision = Policy().decide(tool, {"command": "python -m unknown_module"})
    assert decision.action == ASK
    assert decision.reason == "cannot determine what the shell command does"
    assert not decision.smart_eligible
