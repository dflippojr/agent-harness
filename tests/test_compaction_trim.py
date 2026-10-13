"""Rule-based trimming ahead of elide and summary (#637): each rule, the protected parts, pairing and idempotence."""

from __future__ import annotations

import json

import pytest

from harness import compaction
from harness.compaction import TrimRules
from harness.config import _trim_rules

ON = TrimRules()
OFF = TrimRules(strip_noise=False, head_tail=False, superseded_reads=False, dedupe=False)
HEAD = [{"role": "system", "content": "sys"}, {"role": "user", "content": "fix the build"}]


def turn(i: int, name: str, output: str, **args) -> list[dict]:
    call = {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return [{"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": output}]


def session(*turns: list[dict]) -> list[dict]:
    """HEAD, the given turns, then a final turn that is always kept verbatim."""
    out = list(HEAD)
    for t in turns:
        out += t
    return out + turn(99, "list_files", "a.txt", path=".")


def only(**on) -> TrimRules:
    return TrimRules(**{**OFF.__dict__, **on})


def tools(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]


def all_ok(messages: list[dict]) -> dict:
    return {m["tool_call_id"]: {"ok": True} for m in messages if m["role"] == "tool"}


def trim(messages, rules=ON, keep_chars=0, outcomes=None):
    """compaction.trim, with every call succeeded unless `outcomes` says otherwise, checked for idempotence and
    pairing."""
    outcomes = all_ok(messages) if outcomes is None else outcomes
    out, saved = compaction.trim(messages, rules, keep_chars, outcomes)
    again, saved_again = compaction.trim(out, rules, keep_chars, outcomes)
    assert (again, saved_again) == (out, 0)  # idempotent: a second pass changes nothing
    assert [m.get("tool_call_id") for m in out] == [m.get("tool_call_id") for m in messages]  # pairing kept
    assert [m["role"] for m in out] == [m["role"] for m in messages]
    assert saved == sum(len(m.get("content") or "") for m in messages) - sum(len(m.get("content") or "") for m in out)
    return out, saved


def test_strip_noise_drops_ansi_carriage_return_overwrites_and_progress_runs():
    raw = ("\x1b[32mPASS\x1b[0m tests/a.py\n"
           "Downloading 10%\rDownloading 55%\rDownloading 100%\r\n"
           " 10%|#####     | 1/10\n 50%|##########| 5/10\n100%|##########| 10/10\n"
           "\x1b]0;title\x07done 3 passed")
    assert compaction.strip_noise(raw) == "PASS tests/a.py\nDownloading 100%\n100%|##########| 10/10\ndone 3 passed"
    out, saved = trim(session(turn(1, "run_shell", raw, command="pytest")), only(strip_noise=True))
    assert tools(out)[0] == compaction.strip_noise(raw) and saved > 0


def test_file_reads_keep_their_noise_like_lines():
    text = "before: 10% #####\nafter: 90% #####\n\x1b[1mbold\x1b[0m\n" * 10
    out, saved = trim(session(turn(1, "read_file", text, path="notes.txt")), only(strip_noise=True))
    assert tools(out)[0] == text and saved == 0


def test_a_trailing_carriage_return_keeps_its_line():
    assert compaction.strip_noise("fatal: compilation failed\r\nnext\r") == "fatal: compilation failed\nnext"
    # A shorter overwrite replaces only its own width, as on a terminal.
    assert compaction.strip_noise("FATAL: missing config.json\r!") == "!ATAL: missing config.json"


def test_a_percentage_without_a_bar_is_not_progress_noise():
    text = "coverage 87%\nbranch 50%\n10% of requests failed validation\n20% of requests failed authorization\nok"
    assert compaction.strip_noise(text) == text


def test_head_tail_keeps_the_ends_of_long_command_output_only():
    lines = "\n".join(f"line {i}" for i in range(500))
    out, _ = trim(session(turn(1, "run_shell", lines, command="make"), turn(2, "read_file", lines, path="big.txt")),
                  only(head_tail=True, head_lines=3, tail_lines=2))
    shell, read = tools(out)[:2]
    assert shell.split("\n") == ["line 0", "line 1", "line 2",
                                 compaction.LINES_OMITTED.format(count=495, head=3, tail=2), "line 498", "line 499"]
    assert read == lines  # file reads are not command output


def test_short_command_output_is_untouched():
    text = "\n".join(str(i) for i in range(101))  # head 40 + tail 60 + the marker line would be no shorter
    out, saved = trim(session(turn(1, "run_shell", text, command="seq")), only(head_tail=True))
    assert tools(out)[0] == text and saved == 0


def test_a_reread_with_the_same_arguments_drops_the_older_read():
    body = "def f():\n    return 1\n" * 20
    out, saved = trim(session(turn(1, "read_file", body, path="./src/a.py"),
                              turn(2, "read_file", body[:50], path="src/a.py", start_line=1, end_line=2),
                              turn(3, "read_file", body, path="src\\a.py")), only(superseded_reads=True))
    first, ranged, latest = tools(out)[:3]
    assert first == compaction.SUPERSEDED.format(path="src/a.py") and saved > 0
    assert ranged == body[:50] and latest == body  # another range doesn't supersede; the newest read stays


def test_a_failed_reread_keeps_the_older_read():
    body = "secret = 1\n" * 50
    messages = session(turn(1, "read_file", body, path="a.py"), turn(2, "read_file", "Error: access denied", path="a.py"))
    out, _ = trim(messages, only(superseded_reads=True), outcomes={"c1": {"ok": True}, "c2": {"ok": False}})
    assert tools(out)[0] == body


def test_a_reread_cut_by_the_turn_budget_keeps_the_older_read():
    body = "x = 1\n" * 500
    cut = body[:2000] + f"\n... {compaction.OUTPUT_CUT}2000 characters: this turn's tool results would overflow ...]"
    messages = session(turn(1, "read_file", body, path="a.py"), turn(2, "read_file", cut, path="a.py"))
    out, _ = trim(messages, only(superseded_reads=True))
    assert tools(out)[0] == body


def test_a_call_without_an_outcome_supersedes_nothing():
    """A fork inherits its parent's context without the tool_result events."""
    body = "x = 1\n" * 50
    messages = session(turn(1, "read_file", body, path="a.py"), turn(2, "read_file", body, path="a.py"),
                       turn(3, "read_file", body, path="b.py"), turn(4, "write_file", "?", path="b.py", content="z"))
    out, _ = trim(messages, only(superseded_reads=True), outcomes={})
    assert tools(out)[:3] == [body, body, body]


def test_only_the_workspace_prefix_is_dropped_from_paths():
    body = "x = 1\n" * 50
    messages = session(turn(1, "read_file", body, path="workspace/src/a.py"), turn(2, "read_file", body, path="/src/b.py"),
                       turn(3, "edit_file", "Edited", path="/workspace/src/a.py", old_text="x", new_text="y"),
                       turn(4, "edit_file", "Edited", path="src/b.py", old_text="x", new_text="y"),
                       turn(5, "read_file", body, path="/workspace/src/c.py"),
                       turn(6, "write_file", "Wrote", path="src/c.py", content="z"))
    out, _ = trim(messages, only(superseded_reads=True))
    assert tools(out)[0] == body and tools(out)[1] == body  # different files from the ones edited
    assert tools(out)[4] == compaction.SUPERSEDED.format(path="src/c.py")


def test_a_successful_write_or_edit_supersedes_an_earlier_read_and_a_failed_one_does_not():
    body = "x = 1\n" * 50
    messages = session(turn(1, "read_file", body, path="a.py"), turn(2, "read_file", body, path="b.py"),
                       turn(3, "edit_file", "Edited a.py", path="a.py", old_text="x", new_text="y"),
                       turn(4, "write_file", "Error: denied", path="b.py", content="z"))
    out, _ = trim(messages, only(superseded_reads=True), outcomes={"c3": {"ok": True}, "c4": {"ok": False}})
    assert tools(out)[0] == compaction.SUPERSEDED.format(path="a.py")
    assert tools(out)[1] == body


def test_a_path_through_dotdot_matches_nothing():
    """`link/../a.txt` reads nested/a.txt when `link` is a symlink to nested/subdir, not ./a.txt."""
    body = "x = 1\n" * 50
    messages = session(turn(1, "read_file", body, path="link/../a.txt"),
                       turn(2, "edit_file", "Edited", path="a.txt", old_text="x", new_text="y"),
                       turn(3, "read_file", body, path="./b//c.txt"), turn(4, "write_file", "Wrote", path="b/c.txt"))
    out, _ = trim(messages, only(superseded_reads=True))
    assert tools(out)[0] == body
    assert tools(out)[2] == compaction.SUPERSEDED.format(path="b/c.txt")


def test_repeated_identical_outputs_collapse_to_the_newest_copy():
    status = "On branch main\n" + "modified: src/file_%d.py\n" * 20
    small = "ok"
    out, saved = trim(session(turn(1, "run_shell", status, command="git status"), turn(2, "run_shell", small, command="true"),
                              turn(3, "run_shell", status, command="git status"), turn(4, "run_shell", small, command="true")),
                      only(dedupe=True))
    note = compaction.SAME_AS_BELOW.format(name="run_shell")
    assert tools(out)[:4] == [note, small, status, small]
    assert saved == len(status) - len(note)


def test_a_copy_in_the_kept_tail_is_the_one_kept():
    """A summary removes the oldest messages, so the kept copy must be the newest: never a note without its copy."""
    status = "On branch main\n" + "modified: src/file.py\n" * 20
    old = turn(1, "run_shell", status, command="git status")
    recent = turn(2, "run_shell", status, command="git status")
    messages = HEAD + old + recent + turn(99, "list_files", "a.txt", path=".")
    keep = sum(compaction.message_chars(m) for m in messages[4:])
    out, _ = trim(messages, only(dedupe=True), keep_chars=keep)
    assert tools(out)[:2] == [compaction.SAME_AS_BELOW.format(name="run_shell"), status]


def test_identical_reads_of_different_files_are_not_deduped():
    """A note pointing at a read could outlive it: a later edit makes superseded_reads drop that read."""
    body = "same contents\n" * 30
    messages = session(turn(1, "read_file", body, path="a.txt"), turn(2, "read_file", body, path="b.txt"),
                       turn(3, "edit_file", "Edited a.txt", path="a.txt", old_text="s", new_text="t"))
    out, _ = trim(messages)
    assert tools(out)[:2] == [compaction.SUPERSEDED.format(path="a.txt"), body]


def test_the_head_the_recent_tail_and_receipts_are_never_touched():
    noisy = "\x1b[31m" + "error\n" * 300 + "\x1b[0m"
    receipt = compaction.RECEIPT_PREFIX + " tool=run_shell " + "\x1b[0m" * 50
    head = [{"role": "system", "content": noisy}, {"role": "user", "content": noisy}]
    old = turn(1, "run_shell", noisy, command="a") + turn(2, "run_shell", receipt, command="b")
    recent = turn(3, "run_shell", noisy, command="c")
    newest = turn(4, "run_shell", noisy, command="d")
    messages = head + old + recent + newest
    keep = sum(compaction.message_chars(m) for m in recent + newest)
    out, saved = trim(messages, keep_chars=keep)
    assert out[0] == head[0] and out[1] == head[1]
    assert out[3]["content"] != noisy and out[5]["content"] == receipt
    assert out[6:] == recent + newest and saved > 0
    # With nothing to keep but the newest turn, its unread results still stay as they are.
    out, _ = trim(messages, keep_chars=0)
    assert out[-1]["content"] == noisy


def test_every_rule_off_changes_nothing():
    body = "\x1b[1m" + "line\n" * 300
    messages = session(turn(1, "read_file", body, path="a"), turn(2, "read_file", body, path="a"),
                       turn(3, "run_shell", body, command="x"))
    out, saved = trim(messages, OFF)
    assert out == messages and saved == 0


def test_rules_compose_and_stay_idempotent():
    log = "\x1b[33m" + "".join(f"step {i}\r step {i} done\n" for i in range(300)) + "\x1b[0m"
    messages = session(turn(1, "run_shell", log, command="build"), turn(2, "run_shell", log, command="build"),
                       turn(3, "read_file", "a\n" * 200, path="a.py"), turn(4, "write_file", "Wrote a.py", path="a.py"))
    out, saved = trim(messages)
    first, second, read = tools(out)[:3]
    assert first == compaction.SAME_AS_BELOW.format(name="run_shell")
    assert "\x1b" not in second and "\r" not in second and compaction.LINES_OMITTED.split("{")[0] in second
    assert read == compaction.SUPERSEDED.format(path="a.py") and saved > 0


@pytest.mark.parametrize(("raw", "expected"), [
    (None, TrimRules()),
    ("nope", TrimRules()),
    ({"dedupe": False, "head_lines": 5, "tail_lines": 0}, TrimRules(dedupe=False, head_lines=5, tail_lines=0)),
    ({"dedupe": "false", "strip_noise": 0, "head_lines": True, "tail_lines": 10_001}, TrimRules()),
    ({"head_lines": -1, "tail_lines": "7"}, TrimRules()),
])
def test_config_switches_fall_back_to_the_defaults(raw, expected):
    assert _trim_rules(raw) == expected
