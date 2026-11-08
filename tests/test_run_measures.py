"""Shared per-run measures (bakeoff/run_measures.py, #942): what shell commands read and write, for both drivers.

Each case goes through both drivers' real entry points: Pi's JSON transcript parser (a `bash` call) and the native
session's events (a `run_shell` call), which both feed `file_access`."""

from __future__ import annotations

import json

import pytest

from bakeoff import pi_driver
from bakeoff.run_measures import file_access, native_measures

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


# the shared shell parser and native measures, moved from tests/test_canary_pi_driver.py
def test_a_run_that_read_everything_it_edited_and_one_that_edited_nothing():
    calls = [("read", {"path": "calc.py"}), ("edit", {"path": "./calc.py"})]
    assert file_access(calls, EXISTING) == {"read_before_edit": True, "edited_unread": []}
    assert file_access([("read", {"path": "calc.py"})], EXISTING) == {"read_before_edit": None, "edited_unread": []}
    # naming a file to run it is not reading it
    assert file_access([("bash", {"command": "python calc.py"}), ("edit", {"path": "calc.py"})],
                       EXISTING)["edited_unread"] == ["calc.py"]
    assert file_access([("bash", {"command": "sed -n '1,40p' calc.py && pytest"}), ("edit", {"path": "calc.py"})],
                       EXISTING)["read_before_edit"] is True
    assert file_access([("bash", {"command": "cat 'unbalanced"}), ("write", {"path": "calc.py"})],
                       EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["cat calc.py > /tmp/copy && printf x > calc.py",
                                     "grep -n add < calc.py; printf x > calc.py"])
def test_a_shell_command_that_shows_a_file_before_writing_it_reads_first(command):
    assert file_access([("run_shell", {"command": command})], EXISTING) == {"read_before_edit": True,
                                                                             "edited_unread": []}


@pytest.mark.parametrize("command", ["printf fixed > calc.py; cat calc.py", "cat calc.py > calc.py",
                                     "printf x | tee calc.py && head calc.py"])
def test_a_shell_command_that_writes_a_file_before_showing_it_did_not_read_it_first(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


def test_a_comment_keeps_the_newline_that_ends_its_command():
    assert file_access([("bash", {"command": "cat calc.py # inspect\nprintf fixed > calc.py"})], EXISTING) == {
        "read_before_edit": True, "edited_unread": []}
    # `#` inside quotes or a word is no comment
    assert file_access([("bash", {"command": "echo '# x' > calc.py # note"})], EXISTING)["edited_unread"] == ["calc.py"]
    assert file_access([("bash", {"command": "echo a#b > util.py"})], EXISTING)["edited_unread"] == ["util.py"]


@pytest.mark.parametrize("command", ["cp staging/calc.py .", "mv /tmp/out/calc.py ./", "cp -f a.py calc.py",
                                     "cp new/calc.py /workspace/"])
def test_cp_or_mv_into_the_workspace_overwrites_the_task_file_of_that_name(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["grep calc.py README.md", "rg -n calc.py tests", "awk '/calc.py/' notes.md",
                                     "sed -n /calc.py/p notes.md", "grep -A 3 -e calc.py notes.md",
                                     "head -n calc.py notes.md"])
def test_a_pattern_or_option_value_naming_a_file_is_not_reading_it(command):
    calls = [("bash", {"command": f"{command}; printf fixed > calc.py"})]
    assert file_access(calls, EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["grep -n add calc.py", "grep -e add -e sub calc.py", "rg -A 3 add calc.py",
                                     "sed -n 1,20p calc.py", "awk -F, '{print}' calc.py", "head -n 5 calc.py",
                                     "tail -n 20 calc.py", "cat -- calc.py"])
def test_a_viewer_s_file_operands_are_reads(command):
    assert file_access([("bash", {"command": f"{command}; printf fixed > calc.py"})], EXISTING)["read_before_edit"]


def test_quoted_or_escaped_operators_are_data():
    assert file_access([("bash", {"command": "grep '>' calc.py"})], EXISTING)["read_before_edit"] is None
    calls = [("bash", {"command": "printf '%s' '<' calc.py; printf fixed > calc.py"})]
    assert file_access(calls, EXISTING)["edited_unread"] == ["calc.py"]
    assert file_access([("bash", {"command": 'grep "a|b;c" calc.py && printf x > calc.py'})], EXISTING)[
        "read_before_edit"] is True
    assert file_access([("bash", {"command": "echo \\> calc.py"})], EXISTING)["read_before_edit"] is None
    # a line continuation joins the words around it
    assert file_access([("bash", {"command": "cat \\\n  calc.py; printf x > calc.py"})], EXISTING)[
        "read_before_edit"] is True


@pytest.mark.parametrize("command", ["sed -n -e'/fix/p' calc.py", "sed -ne '/i/p' calc.py", "sed -n -f fix.sed calc.py"])
def test_an_i_inside_an_attached_script_is_not_in_place(command):
    assert file_access([("bash", {"command": f"{command}; printf x > calc.py"})], EXISTING)["read_before_edit"] is True


@pytest.mark.parametrize("command", ["cat <<< hello\nprintf fixed > calc.py", "cat <<<EOF\nprintf fixed > calc.py",
                                     "cat <<EOF-MARKER\nEOF\nx\nEOF-MARKER\nprintf fixed > calc.py",
                                     "cat <<'END.TXT'\nprintf no > util.py\nEND.TXT\nprintf fixed > calc.py",
                                     "cat <<\\EOF\nprintf no > util.py\nEOF\nprintf fixed > calc.py"])
def test_here_strings_are_no_here_document_and_delimiters_are_whole_words(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["echo hi # <<EOF\nprintf fixed > calc.py", "echo '<<EOF'\nprintf fixed > calc.py",
                                     'echo "a <<EOF b"\nprintf fixed > calc.py'])
def test_a_here_document_marker_in_a_comment_or_quotes_is_none(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["grep --color add calc.py", "grep --color=always add calc.py",
                                     "rg --sort path add calc.py", "rg --color never add calc.py"])
def test_optional_and_required_option_values_leave_the_file_operand(command):
    assert file_access([("bash", {"command": f"{command}; printf x > calc.py"})], EXISTING)["read_before_edit"] is True


@pytest.mark.parametrize("command", ["rg --sort path calc.py notes.md", "grep --color calc.py notes.md"])
def test_a_pattern_after_an_option_value_is_still_a_pattern(command):
    assert file_access([("bash", {"command": f"{command}; printf x > calc.py"})], EXISTING)["edited_unread"] == [
        "calc.py"]


@pytest.mark.parametrize("command", ["grep --include '*.md' calc.py notes.md", "grep --exclude-dir .git -r calc.py .",
                                     "rg --glob '*.py' calc.py", "grep --max-count 1 calc.py notes.md"])
def test_long_options_that_take_a_value_keep_the_pattern_apart(command):
    assert file_access([("bash", {"command": f"{command}; printf x > calc.py"})], EXISTING)["edited_unread"] == [
        "calc.py"]


def test_a_cd_inside_a_subshell_ends_with_it():
    existing = {"inventory/report.py", "report.py"}
    calls = [("bash", {"command": "(cd inventory; cat report.py); printf fixed > report.py"})]
    assert file_access(calls, existing) == {"read_before_edit": False, "edited_unread": ["report.py"]}
    calls = [("bash", {"command": "cd inventory && (cd /tmp; cat report.py) && sed -i s/a/b/ report.py"})]
    assert file_access(calls, existing)["edited_unread"] == ["inventory/report.py"]


@pytest.mark.parametrize("command", ["cp -t . staging/calc.py", "mv --target-directory=/workspace out/calc.py",
                                     "cp -t/workspace new/calc.py", "cp --target-directory . -f staging/calc.py"])
def test_cp_or_mv_with_a_target_directory_overwrites_the_task_file_of_that_name(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


@pytest.mark.parametrize("command", ["printf x\n>calc.py", "echo x;>calc.py", "true&&>calc.py", "(echo x)>calc.py",
                                     "echo hi &> calc.py", "echo hi &>>calc.py"])
def test_a_redirect_next_to_other_punctuation_is_still_a_write(command):
    assert file_access([("bash", {"command": command})], EXISTING)["edited_unread"] == ["calc.py"]


def test_here_document_bodies_are_data_not_commands():
    heredoc = "cat > new.py <<'EOF'\nopen('calc.py', 'w').write(x > util.py)\nEOF\ncat calc.py\nsed -i s/a/b/ calc.py"
    assert file_access([("bash", {"command": heredoc})], EXISTING) == {"read_before_edit": True,
                                                                        "edited_unread": []}
    tabbed = "python - <<-PY\n\tprint(1) > calc.py\n\tPY\nprintf x > util.py"
    assert file_access([("bash", {"command": tabbed})], EXISTING)["edited_unread"] == ["util.py"]


def test_shell_paths_follow_cd():
    existing = {"inventory/report.py", "report.py"}
    assert file_access([("bash", {"command": "cd inventory && printf fixed > report.py"})], existing) == {
        "read_before_edit": False, "edited_unread": ["inventory/report.py"]}
    assert file_access([("bash", {"command": "cd /workspace/inventory; cat report.py; sed -i s/a/b/ report.py"})],
                       existing) == {"read_before_edit": True, "edited_unread": []}
    assert file_access([("bash", {"command": "cd inventory; cd ..; printf x > report.py"})], existing)[
        "edited_unread"] == ["report.py"]
    for elsewhere in ("cd /tmp && printf x > report.py", "cd && printf x > report.py", "cd ~; cp a report.py"):
        assert file_access([("bash", {"command": elsewhere})], existing)["read_before_edit"] is None, elsewhere
    # each call starts in the workspace again
    assert file_access([("bash", {"command": "cd inventory"}), ("bash", {"command": "printf x > report.py"})],
                       existing)["edited_unread"] == ["report.py"]


@pytest.mark.parametrize("command", ["pytest -q 2>&1 | tail -n 20", "python calc.py > /dev/null", "git diff calc.py",
                                     "printf x > new_file.py"])
def test_shell_commands_that_write_no_task_file_are_no_edit(command):
    assert file_access([("bash", {"command": command})], EXISTING)["read_before_edit"] is None


def test_native_measures_come_from_the_session_events():
    events = [{"type": "tool_call", "data": {"name": "read_file", "args": {"path": "calc.py"}}},
              {"type": "turn_metrics", "data": {"cache_tokens": None}},
              {"type": "tool_call", "data": {"name": "edit_file", "args": {"path": "calc.py"}}},
              {"type": "turn_metrics", "data": {"cache_tokens": 2900}},
              {"type": "tool_call", "data": {"name": "write_file", "args": {"path": "util.py"}}},
              {"type": "tool_call", "data": {"name": "run_shell", "args": {"command": "pytest -q"}}},
              {"type": "turn_metrics", "data": {"cache_tokens": 3100}}]
    m = native_measures({"totals": {"completion_tokens": 321}}, events, EXISTING)
    assert m == {"driver": "native", "completion_tokens": 321, "cached_prompt_tokens": 6000,
                 "tool_calls": {"edit_file": 1, "read_file": 1, "run_shell": 1, "write_file": 1},
                 "read_before_edit": False, "edited_unread": ["util.py"]}
    assert native_measures({"totals": {}}, [], set())["cached_prompt_tokens"] is None


# `cd -` goes back to the previous directory
@DRIVERS
@pytest.mark.parametrize("command", ["cd tests; cd -; printf fixed > calc.py", "cd tests && cd - >/dev/null && printf x > calc.py",
                                     "cd tests; cd ..; cd -; cd -; printf x > calc.py",
                                     "cd tests; (cd -; printf x > calc.py)"])
def test_cd_dash_returns_to_the_previous_directory(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
def test_cd_dash_with_no_previous_directory_stays_put(measure):
    # a fresh shell has no OLDPWD: `cd -` fails and the command stays where it was
    assert measure("cd -; printf x > calc.py") == {"read_before_edit": False, "edited_unread": ["calc.py"]}
    existing = {"calc.py", "tests/calc.py"}
    assert measure("cd tests; cd ..; cd -; printf x > calc.py", existing) == {"read_before_edit": False,
                                                                               "edited_unread": ["tests/calc.py"]}


# a subshell's trailing redirects with descriptor numbers (`2>`, `1>`, `2>&1`) still open before its body
@DRIVERS
@pytest.mark.parametrize("command", ["(cat calc.py) 2>errors.log >calc.py", "(cat calc.py) 2>&1 >calc.py",
                                     "(cat calc.py) 1>calc.py", "(cat calc.py) 2>/dev/null > calc.py",
                                     "(cat calc.py) 2> errors.log 1> calc.py"])
def test_a_subshell_s_descriptor_redirects_open_before_its_body(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
@pytest.mark.parametrize("command", ["(cat calc.py) 2>errors.log; printf x > calc.py",
                                     "(cat calc.py) 2>&1; printf x 1> calc.py"])
def test_a_subshell_with_only_descriptor_redirects_still_reads_first(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


# an agent's shell syntax error never takes the run down
@DRIVERS
@pytest.mark.parametrize("command", ["(cat calc.py) >", "(cat calc.py) 2>", "(cat calc.py) 2", "(cat calc.py) >&",
                                     "cat calc.py >", "cat calc.py <", ">", "2>", "(", ")", "((", "))", ") > calc.py",
                                     "cd", "cd -", "cat <<", "cat <<EOF", "'", '"', "\\", "cp -t", "sed -i", "|", ";;",
                                     "(cd tests; cat calc.py) 2>&", "x\n>", "", "   "])
def test_a_malformed_command_measures_without_raising(measure, command):
    result = measure(command)
    assert result["read_before_edit"] in (True, False, None) and isinstance(result["edited_unread"], list)


@DRIVERS
def test_a_dangling_redirect_keeps_the_command_before_it(measure):
    # `cat calc.py >` is a syntax error, but what it names before the dangling `>` is still parsed
    assert measure("cat calc.py >; printf x > calc.py") == {"read_before_edit": True, "edited_unread": []}
    assert measure("(cat calc.py) >; printf x > calc.py") == {"read_before_edit": True, "edited_unread": []}


# descriptor numbers are redirect syntax in plain commands too, never operands
@DRIVERS
@pytest.mark.parametrize("command", ["cp fixed.py calc.py 2>errors.log", "cp fixed.py calc.py 2>&1",
                                     "mv out/a.py calc.py 2>/dev/null", "printf x 1>calc.py",
                                     "cp fixed.py calc.py 2> errors.log 1>&2", "sed -i s/a/b/ calc.py 2>&1 | tail -n 3"])
def test_a_descriptor_redirect_on_a_command_is_no_operand(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
@pytest.mark.parametrize("command", ["cat calc.py 2>&1 | head; printf x > calc.py",
                                     "grep add calc.py 2>/dev/null && printf x > calc.py",
                                     "head -n 5 calc.py 2>errors.log; printf x 1> calc.py"])
def test_a_viewer_with_a_descriptor_redirect_still_reads(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


# each driver's file tools resolve paths their own way
def _native_calls(*calls: tuple[str, dict]) -> dict:
    events = [{"type": "tool_call", "data": {"name": name, "args": args}} for name, args in calls]
    m = native_measures({"totals": {}}, events, EXISTING)
    return {"read_before_edit": m["read_before_edit"], "edited_unread": m["edited_unread"]}


@pytest.mark.parametrize("path", ["/calc.py", "//calc.py", "/workspace/calc.py", "./calc.py", "tests/../calc.py"])
def test_native_file_tools_read_a_leading_slash_as_the_workspace_as_fileops_does(path):
    from harness.fileops import normalize_path
    assert normalize_path(path).lstrip("./").replace("tests/../", "") == "calc.py"  # the tools' own rule
    assert _native_calls(("write_file", {"path": path, "content": "x"})) == {"read_before_edit": False,
                                                                              "edited_unread": ["calc.py"]}
    assert _native_calls(("read_file", {"path": path}), ("edit_file", {"path": "calc.py"})) == {
        "read_before_edit": True, "edited_unread": []}
    assert _native_calls(("read_file", {"path": "calc.py"}), ("write_file", {"path": path})) == {
        "read_before_edit": True, "edited_unread": []}


def test_pi_file_tools_take_a_leading_slash_as_the_filesystem_root():
    # Pi's read/edit/write resolve like any path: /calc.py is outside the workspace (/workspace/calc.py is inside)
    def pi_calls(*calls):
        records = []
        for i, (name, args) in enumerate(calls):
            records += [{"type": "tool_execution_start", "toolCallId": f"c{i}", "toolName": name, "args": args},
                        {"type": "turn_end", "message": {"role": "assistant"}, "toolResults": []}]
        m = pi_driver.parse_transcript("".join(json.dumps(r) + "\n" for r in records), EXISTING)
        return {"read_before_edit": m["read_before_edit"], "edited_unread": m["edited_unread"]}

    assert pi_calls(("write", {"path": "/calc.py", "content": "x"})) == {"read_before_edit": None,
                                                                          "edited_unread": []}
    assert pi_calls(("write", {"path": "/workspace/calc.py", "content": "x"})) == {"read_before_edit": False,
                                                                                    "edited_unread": ["calc.py"]}
    assert pi_calls(("read", {"path": "/workspace/calc.py"}), ("edit", {"path": "./tests/../calc.py"})) == {
        "read_before_edit": True, "edited_unread": []}


# a command substitution expands before the enclosing command's redirects open
@DRIVERS
@pytest.mark.parametrize("command", ["echo $(cat calc.py) > calc.py", 'echo "$(cat calc.py)" > calc.py',
                                     "echo `cat calc.py` > calc.py", 'echo "`cat calc.py`" >calc.py',
                                     'x=$(cat calc.py); printf %s "$x" > calc.py', "echo $(head -n 3 calc.py)>calc.py",
                                     'printf "%s\\n" "a $(grep add calc.py) b" > calc.py'])
def test_a_command_substitution_reads_before_the_redirect_opens(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


@DRIVERS
@pytest.mark.parametrize("command", ["printf x > calc.py; echo $(cat calc.py)", "(cat calc.py) > calc.py",
                                     "echo $(printf x > calc.py; cat calc.py)", "echo '$(cat calc.py)' > calc.py"])
def test_a_write_before_the_substitution_or_a_quoted_dollar_is_still_unread(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
def test_a_cd_inside_a_command_substitution_ends_with_it(measure):
    existing = {"calc.py", "tests/calc.py"}
    assert measure("echo $(cd tests; cat calc.py) > calc.py", existing) == {"read_before_edit": False,
                                                                            "edited_unread": ["calc.py"]}


# leading assignments and command wrappers don't hide the command
@DRIVERS
@pytest.mark.parametrize("command", ["LC_ALL=C sed -i s/a/b/ calc.py", "A=1 B='x y' cp new.py calc.py",
                                     "env LC_ALL=C sed -i s/a/b/ calc.py", "env -u HOME tee calc.py < notes.md",
                                     "command cp new.py calc.py", "nice -n 5 sed -i s/a/b/ calc.py",
                                     "timeout -k 5 30s cp new.py calc.py", "time truncate -s 0 calc.py",
                                     "nohup sudo -E mv new.py calc.py", "exec cp new.py calc.py"])
def test_an_assignment_or_wrapper_before_a_write_still_writes(measure, command):
    assert measure(command)["edited_unread"] == ["calc.py"]


@DRIVERS
@pytest.mark.parametrize("command", ["LC_ALL=C cat calc.py; printf x > calc.py",
                                     "env LANG=C grep -n add calc.py && printf x > calc.py",
                                     "timeout 5 head -n 3 calc.py; printf x > calc.py",
                                     "X=1 cd tests && cd .. && cat calc.py; printf x > calc.py"])
def test_an_assignment_or_wrapper_before_a_viewer_still_reads(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


@DRIVERS
def test_a_bare_assignment_is_no_command(measure):
    assert measure("X=calc.py; printf x > notes.md") == {"read_before_edit": False, "edited_unread": ["notes.md"]}


# a wrapper that changes directory (env -C, sudo -D) runs only its command there
@DRIVERS
@pytest.mark.parametrize("command", ["env -C tests cat calc.py; printf fixed > calc.py",
                                     "env --chdir=tests cat calc.py; printf x > calc.py",
                                     "env -Ctests cat calc.py; printf x > calc.py",
                                     "sudo -D tests cat calc.py; printf x > calc.py",
                                     "env --chdir tests cat calc.py > ../out.txt; printf x > calc.py"])
def test_a_chdir_wrapper_resolves_its_command_s_paths_there(measure, command):
    existing = {"calc.py", "tests/calc.py"}
    assert measure(command, existing) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
def test_a_chdir_wrapper_leaves_the_shell_s_directory_and_redirects_alone(measure):
    existing = {"calc.py", "tests/calc.py"}
    # the redirect opens in the shell's directory, and the next command runs there too
    assert measure("env -C tests cat calc.py > calc.py", existing) == {"read_before_edit": False,
                                                                       "edited_unread": ["calc.py"]}
    assert measure("env -C tests sed -i s/a/b/ calc.py; cat calc.py", existing) == {
        "read_before_edit": False, "edited_unread": ["tests/calc.py"]}


# cp/mv -t inside a cluster of short options
@DRIVERS
@pytest.mark.parametrize("command", ["cp -ft . staging/calc.py", "mv -ft . staging/calc.py", "cp -vft. staging/calc.py",
                                     "cp -rt /workspace staging/calc.py", "mv -fvt . a/x.py staging/calc.py"])
def test_a_target_directory_inside_an_option_cluster_is_still_the_destination(measure, command):
    assert measure(command)["edited_unread"] == ["calc.py"]


@DRIVERS
def test_a_cluster_without_t_keeps_the_last_operand_as_the_destination(measure):
    assert measure("cp -fv staging/calc.py calc.py") == {"read_before_edit": False, "edited_unread": ["calc.py"]}
    assert measure("cp -fv calc.py staging/") == {"read_before_edit": None, "edited_unread": []}


# a word with a substitution glued into it is not known, so it names no task file
@DRIVERS
@pytest.mark.parametrize("command", ["cat calc.py$(printf .bak); printf fixed > calc.py",
                                     "cat $(printf calc).py; printf x > calc.py",
                                     "cat calc.py`echo .bak`; printf x > calc.py",
                                     'cat "calc.py$(echo .bak)"; printf x > calc.py',
                                     "head -n 3 x$(echo)calc.py; printf x > calc.py"])
def test_a_word_with_a_glued_substitution_names_no_known_file(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
@pytest.mark.parametrize("command", ["cat $(echo x) calc.py; printf x > calc.py",
                                     "cat calc.py $(echo x); printf x > calc.py",
                                     "echo $(cat calc.py)>calc.py"])
def test_a_substitution_beside_a_word_leaves_the_word_known(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}


# a cd in a pipeline runs in that pipeline's subshell
@DRIVERS
@pytest.mark.parametrize("command", ["cat calc.py; cd tests | cat; printf x > calc.py",
                                     "cat calc.py; true | cd tests; printf x > calc.py",
                                     "cat calc.py; cd tests | cd .. | cat; printf x > calc.py"])
def test_a_cd_in_a_pipeline_leaves_the_shell_where_it_was(measure, command):
    assert measure(command, {"calc.py", "tests/calc.py"}) == {"read_before_edit": True, "edited_unread": []}


@DRIVERS
def test_a_cd_before_a_pipeline_still_moves_the_shell(measure):
    existing = {"calc.py", "tests/calc.py"}
    assert measure("cd tests && cat calc.py | head; printf x > calc.py", existing) == {"read_before_edit": True,
                                                                                       "edited_unread": []}
    assert measure("cd tests | cat; cat tests/calc.py; printf x > tests/calc.py", existing) == {
        "read_before_edit": True, "edited_unread": []}


# pipeline stages run at the same time: a read in one stage proves nothing about another stage's write
@DRIVERS
@pytest.mark.parametrize("command", ["(sleep 0.2; cat calc.py) | tee calc.py", "cat calc.py | tee calc.py",
                                     "grep add calc.py |& tee calc.py", "cat calc.py | cat | tee -a calc.py",
                                     "tee calc.py < notes.md | cat calc.py", "(cd tests; cat ../calc.py) | tee calc.py"])
def test_a_read_in_one_pipeline_stage_does_not_precede_another_stage_s_write(measure, command):
    assert measure(command) == {"read_before_edit": False, "edited_unread": ["calc.py"]}


@DRIVERS
@pytest.mark.parametrize("command", ["cat calc.py; cat notes.md | tee calc.py",
                                     "(cat calc.py; printf x > calc.py) | cat",
                                     "cat calc.py | tee out.txt; printf x > calc.py",
                                     "cat calc.py && (head notes.md | tee calc.py)"])
def test_order_within_a_stage_and_before_the_pipeline_still_counts(measure, command):
    assert measure(command) == {"read_before_edit": True, "edited_unread": []}
