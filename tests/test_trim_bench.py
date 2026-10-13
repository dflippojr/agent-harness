"""The #637 trim benchmark replays its synthetic transcripts the way the runner compacts (bakeoff/trim_bench.py)."""

from __future__ import annotations

from bakeoff import trim_bench


def test_trim_cuts_summary_calls_on_the_exploration_transcript():
    steps = trim_bench.exploration()
    base, trim = (trim_bench.replay(steps, arm, mask=False, n=32768) for arm in trim_bench.ARMS[:2])
    assert base.summary_calls > trim.summary_calls and trim.trim_chars > 0 and base.trim_chars == 0
    assert trim.elide_only == trim.passes and base.elide_only < base.passes


def test_needed_again_counts_recurring_head_tail_trims():
    r = trim_bench.replay(trim_bench.build_failures(), trim_bench.ARMS[1], mask=False, n=32768)
    assert r.head_tail and 0 < r.needed_again <= len(r.head_tail)  # make reruns after every fix


def test_table_lists_every_transcript_and_arm(capsys):
    trim_bench.main(["--context", "32768"])
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("| ") and "---" not in line]
    assert len(rows) == 1 + len(trim_bench.TRANSCRIPTS) * len(trim_bench.ARMS)
