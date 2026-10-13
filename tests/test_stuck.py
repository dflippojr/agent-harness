"""Stuck caps (#643): repeated identical failing calls and consecutive invalid tool calls, on scripted sequences."""

from __future__ import annotations

from harness import stuck
from harness.runner import new_run

CAPS = stuck.Caps(nudge_after=3, stop_after=8, invalid_after=5)
FAIL = "Error: no such file: missing.md"
ARGS = '{"path": "missing.md"}'


def feed(run: dict, steps: list[tuple], caps: stuck.Caps = CAPS) -> list[tuple[str, dict | None]]:
    """Each step is (name, arguments, output, ok[, invalid])."""
    return [stuck.observe(run, caps, *step) for step in steps]


def test_nudge_from_the_nth_identical_failure_and_stop_at_the_mth():
    run = new_run()
    results = feed(run, [("read_file", ARGS, FAIL, False)] * 8)
    notes = [note for note, _ in results]
    assert notes[:2] == ["", ""]
    assert "failed 3 times" in notes[2] and "will not work" in notes[2]
    assert all("will not work" in note for note in notes[2:])
    assert [stop for _, stop in results[:7]] == [None] * 7
    assert results[7][1] == {"reason": stuck.STUCK_REPEAT, "tool": "read_file", "count": 8}
    assert stuck.stop_reason(run) == stuck.STUCK_REPEAT


def test_identity_is_name_and_canonical_arguments():
    run = new_run()
    feed(run, [("read_file", '{"path": "missing.md", "limit": 5}', FAIL, False),
               ("read_file", {"limit": 5, "path": "missing.md"}, FAIL, False)])
    note, _ = stuck.observe(run, CAPS, "read_file", '{"limit":5,"path":"missing.md"}', FAIL, False)
    assert "failed 3 times" in note
    # Another path or another tool is a different call.
    assert stuck.observe(run, CAPS, "read_file", '{"path": "other.md"}', FAIL, False) == ("", None)
    assert stuck.observe(run, CAPS, "list_dir", ARGS, FAIL, False) == ("", None)


def test_successes_and_denials_do_not_count():
    run = new_run()
    steps = [("read_file", ARGS, FAIL, False), ("read_file", ARGS, FAIL, False),
             ("read_file", ARGS, "ok", True),
             ("read_file", ARGS, "Error: the user denied this read_file call. Don't retry it.", False),
             ("read_file", ARGS, "Error: blocked by policy (not allowed). Don't retry this.", False),
             ("read_file", ARGS, "Not run: the user cancelled the task.", False)]
    assert all(result == ("", None) for result in feed(run, steps))
    note, _ = stuck.observe(run, CAPS, "read_file", ARGS, FAIL, False)
    assert "failed 3 times" in note


def test_consecutive_invalid_calls_end_the_run_and_a_valid_call_resets():
    run = new_run()
    def bad(i: int) -> tuple:
        return ("write_file", "{" * (i + 1), "Error: tool arguments were not a valid JSON object.", False, True)
    results = feed(run, [bad(i) for i in range(4)] + [("read_file", ARGS, "fine", True)]
                   + [bad(i) for i in range(4, 8)])
    assert all(stop is None for _, stop in results)
    _, stop = stuck.observe(run, CAPS, *bad(8))
    assert stop == {"reason": stuck.STUCK_INVALID, "tool": "write_file", "count": 5}
    assert stuck.stop_reason(run) == stuck.STUCK_INVALID


def test_a_valid_but_failing_call_also_resets_the_invalid_streak():
    run = new_run()
    bad = ("nope", "{}", "Error: unknown tool 'nope'.", False, True)
    feed(run, [bad] * 4 + [("read_file", ARGS, FAIL, False)] + [bad] * 3)
    assert stuck.stop_reason(run) == ""


def test_identical_invalid_calls_also_count_as_repeated_failures():
    run = new_run()
    bad = ("nope", "{}", "Error: unknown tool 'nope'.", False, True)
    feed(run, [bad, bad, ("read_file", ARGS, FAIL, False)] * 4)
    assert stuck.stop_reason(run) == stuck.STUCK_REPEAT


def test_zero_turns_a_cap_off_and_a_stop_is_final():
    run = new_run()
    off = stuck.Caps(nudge_after=0, stop_after=0, invalid_after=0)
    results = feed(run, [("read_file", ARGS, FAIL, False)] * 20 + [("x", "{", "Error: bad", False, True)] * 20, off)
    assert all(result == ("", None) for result in results)
    run = new_run()
    feed(run, [("x", "{", "Error: bad", False, True)] * 5)
    assert stuck.observe(run, CAPS, "read_file", ARGS, FAIL, False) == ("", None)
    assert stuck.stop_reason(run) == stuck.STUCK_INVALID


def test_counters_live_on_the_run_and_a_new_run_starts_clean():
    run = new_run()
    feed(run, [("read_file", ARGS, FAIL, False)] * 8)
    assert "stuck" not in new_run(carry=run)


def test_describe():
    assert stuck.describe({"reason": stuck.STUCK_REPEAT, "tool": "read_file", "count": 8}) == \
        "Run stopped: read_file failed 8 times with the same arguments"
    assert stuck.describe({"reason": stuck.STUCK_INVALID, "tool": "x", "count": 5}) == \
        "Run stopped: 5 invalid tool calls in a row"
