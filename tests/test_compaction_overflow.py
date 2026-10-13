"""Summaries build on the previous one (#640), and a prompt the model server rejects as too long compacts and retries."""

from __future__ import annotations

import asyncio
import json

from harness import compaction, fake_model, llm
from harness.llm import Completion, LLMError
from harness.manager import Manager
from test_daemon import Script, call, events, make_cfg, wait_status

# The pinned llama-server's reply to an oversized prompt, as llm.chat reports it.
OVERFLOW = ('HTTP 400: {"error":{"code":400,"message":"request (40210 tokens) exceeds the available context size '
            '(32768 tokens), try increasing it","type":"exceed_context_size_error","n_prompt_tokens":40210,'
            '"n_ctx":32768}}')
FIRST = "## Findings\ncount=17 in data/a.csv; total 4,512.30\n## Next steps\nsum data/b.csv"


def _turns(n: int, size: int) -> list[dict]:
    out = []
    for i in range(n):
        c = call("read_file", i, path=f"f{i}.txt")
        out += [{"role": "assistant", "content": "", "tool_calls": [c]},
                {"role": "tool", "tool_call_id": c["id"], "content": f"result {i} " + "x" * size}]
    return out


def _summarized(notes: str = "") -> list[dict]:
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "sum the CSVs"}]
    return compaction.apply_summary(head + _turns(2, 10), 2, 6, FIRST, notes=notes) + _turns(12, 5000)


def _prompt(request: list[dict]) -> str:
    assert request[0]["content"] == compaction.SUMMARY_SYSTEM
    return request[1]["content"]


def test_the_previous_summary_survives_an_over_budget_excerpt():
    messages = _summarized(notes="keep me")
    start, end = 2, len(messages) - 2
    budget = 8000
    text = _prompt(compaction.summary_request(messages, start, end, max_chars=budget, notes="keep me"))
    prior, _, excerpt = text.partition("Conversation excerpt:\n")
    assert "Prior notes" in prior and "Update" in prior
    assert FIRST in prior                                   # verbatim, ahead of the excerpt
    assert compaction.SUMMARY_TAG not in text               # framing and saved notes are not summarized again
    assert "keep me" not in text and compaction.SUMMARY_CONTINUE not in text
    assert excerpt.startswith("[earliest part omitted]")    # the excerpt was cut, not the notes
    block = compaction.PRIOR_NOTES.format(prior=FIRST)
    assert len(excerpt) <= budget - len(block) + len("[earliest part omitted]\n")


def test_no_prior_block_without_an_earlier_summary():
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    messages = head + _turns(4, 100)
    text = _prompt(compaction.summary_request(messages, 2, len(messages), max_chars=100000))
    assert "Prior notes" not in text
    assert "result 0" in text


def test_prior_summary_reads_only_summary_messages():
    assert compaction.prior_summary({"role": "user", "content": "plain"}) is None
    assert compaction.prior_summary(_summarized()[2]) == FIRST


def test_only_the_servers_context_error_counts_as_overflow():
    assert llm.is_context_overflow(LLMError(OVERFLOW))
    assert not llm.is_context_overflow(LLMError("HTTP 400: invalid tool schema"))
    assert not llm.is_context_overflow(LLMError("HTTP 500: " + llm.CONTEXT_OVERFLOW, retryable=True))


def _overflow_cfg(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.keep_recent = 0.001  # keep only the newest turn, so a few small turns are enough to summarize
    return cfg


def test_overflow_compacts_once_and_retries(tmp_path):
    turns = {"n": 0}

    def step(_msgs):  # by call, not by assistant turns: the summary removes those
        turns["n"] += 1
        if turns["n"] <= 4:
            return Completion(tool_calls=[call("run_fake", turns["n"])])
        return LLMError(OVERFLOW) if turns["n"] == 5 else Completion(content="final")

    script = Script([step])

    async def body():
        m = Manager(_overflow_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("long task")["id"], "done")
        assert s["answer"] == "final"
        overflow = [c for c in events(m, s["id"], "compaction") if c.get("reason") == "overflow"]
        assert len(overflow) == 1 and overflow[0]["tier"] == "summary"
        assert sum(1 for r in script.requests if r[0]["content"] == compaction.SUMMARY_SYSTEM) == 1
        turns_sent = [r for r in script.requests if r[0]["content"] != compaction.SUMMARY_SYSTEM]
        assert len(turns_sent) == 6                                        # four turns, the rejected call, the retry
        assert turns_sent[-1][2]["content"].startswith(compaction.SUMMARY_TAG)  # the retry got the compacted context
        assert m.db.get_session(s["id"])["run"]["summary"] == "SUMMARY: did things"
        assert not events(m, s["id"], "error")
        await m.stop()
    asyncio.run(body())


def test_a_second_overflow_still_fails_the_run(tmp_path):
    turns = {"n": 0}

    def step(_msgs):  # by call, not by assistant turns: the summary removes those
        turns["n"] += 1
        return Completion(tool_calls=[call("run_fake", turns["n"])]) if turns["n"] <= 4 else LLMError(OVERFLOW)

    script = Script([step])

    async def body():
        m = Manager(_overflow_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("long task")["id"], "failed")
        assert len([c for c in events(m, s["id"], "compaction") if c.get("reason") == "overflow"]) == 1
        turns = [r for r in script.requests if r[0]["content"] != compaction.SUMMARY_SYSTEM]
        assert len(turns) == 6   # four turns, the rejected call and its one retry
        await m.stop()
    asyncio.run(body())


def test_other_client_errors_do_not_compact(tmp_path):
    script = Script([LLMError("HTTP 400: invalid tool schema")])

    async def body():
        m = Manager(_overflow_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("task")["id"], "failed")
        assert not events(m, s["id"], "compaction")
        assert len(script.requests) == 1
        await m.stop()
    asyncio.run(body())


def test_fake_model_overflow_end_to_end(tmp_path):
    """Fake model mode: a scripted context error compacts once, the retry hits it again and the run fails."""
    path = tmp_path / "fixtures.yaml"
    path.write_text(f"""
scripts:
  - name: overflow
    match: "too long"
    turns:
      - tool_calls: [{{name: run_fake, args: {{}}}}]
      - tool_calls: [{{name: run_fake, args: {{}}}}]
      - tool_calls: [{{name: run_fake, args: {{}}}}]
      - error: {{message: {json.dumps(OVERFLOW)}}}
""", encoding="utf-8")

    async def body():
        m = Manager(_overflow_cfg(tmp_path), chat=fake_model.FakeModel.from_path(path))
        await m.start()
        s = await wait_status(m, m.create("this prompt is too long")["id"], "failed")
        overflow = [c for c in events(m, s["id"], "compaction") if c.get("reason") == "overflow"]
        assert [c["tier"] for c in overflow] == ["summary"]
        assert any("exceeds the available context size" in e.get("message", "") for e in events(m, s["id"], "error"))
        await m.stop()
    asyncio.run(body())


def test_prior_notes_over_the_budget_leave_half_of_it_to_the_newer_messages():
    """8,192 tokens at 3 characters each, and a ~12,000-character earlier summary (#654 review): the newer
    messages are removed once summarized, so the summarizer must still see the newest of them."""
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "sum the CSVs"}]
    prior = "## Findings\n" + "count=17 in data/a.csv\n" * 520 + "## Next steps\nsum data/b.csv"
    tail = [{"role": "user", "content": "correction: skip data/c.csv"}, {"role": "assistant", "content": "ok"}]
    messages = compaction.apply_summary(head, 2, 2, prior) + _turns(8, 4000) + tail
    budget = int(0.45 * 8192 * 3)
    assert len(prior) > budget
    text = _prompt(compaction.summary_request(messages, 2, len(messages), max_chars=budget))
    block, _, excerpt = text.partition("Conversation excerpt:\n")
    assert "correction: skip data/c.csv" in excerpt
    assert len(excerpt) >= budget // 2 - len("[earliest part omitted]\n")
    assert "prior notes shortened" in block
    assert "## Findings" in block and "sum data/b.csv" in block   # the start and the next steps stay
    prior_block = block[block.index("Prior notes"):]
    assert len(prior_block) + len(excerpt) <= budget + len("[earliest part omitted]\n")   # within the same total


def test_shorten_keeps_both_ends_within_the_limit():
    assert compaction._shorten("abc", 3) == "abc"
    short = compaction._shorten("a" * 500 + "b" * 500, 200)
    assert len(short) <= 200 and short.startswith("a") and short.endswith("b")
    assert "1000 characters" in compaction._shorten("x" * 1000, 0)


def test_a_summary_quoting_the_notes_heading_is_kept_whole():
    quoting = f"## Findings\nthe template has\n\n{compaction.NOTES_HEADING}\nin it\n## Next steps\nedit it"
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for notes in ("", "my notes"):
        message = compaction.apply_summary(head + _turns(2, 10), 2, 6, quoting, notes=notes)[2]
        assert compaction.prior_summary(message, known=quoting, known_notes=notes) == quoting
    # Without the kept text only the exact saved notes are cut, so a quoted heading never drops what follows it.
    quoted = f"x\n\n{compaction.NOTES_HEADING}\ny"
    message = compaction.apply_summary(head, 2, 2, FIRST, notes=quoted)[2]
    assert compaction.prior_summary(message, notes=quoted) == FIRST
    assert compaction.prior_summary(message).startswith(FIRST)
    for notes in ("", "my notes"):
        message = compaction.apply_summary(head, 2, 2, quoting, notes=notes)[2]
        assert compaction.prior_summary(message, notes=notes) == quoting
        assert compaction.prior_summary(message, notes="edited since").startswith(quoting)
    # Kept text from another summary (a round reset dropped that one) is not used.
    assert compaction.prior_summary(compaction.apply_summary(head, 2, 2, FIRST)[2], known="stale") == FIRST


def test_kept_text_that_only_starts_a_restored_summary_is_not_used():
    """A rewind can restore a longer summary that begins with the kept text and a blank line (#654 review)."""
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    kept = "## Findings\ncount=17"
    restored = f"{kept}\n\n## Next steps\nsum data/b.csv"
    for notes in ("", "my notes"):
        message = compaction.apply_summary(head, 2, 2, restored, notes=notes)[2]
        assert compaction.prior_summary(message, known=kept, notes=notes, known_notes=notes) == restored


def test_a_follow_up_run_keeps_the_summary_that_quotes_the_notes_heading():
    """Continuing a finished session starts a new run; the kept summary text comes with it (#654 review)."""
    from harness.runner import new_run
    tail = "in it\n## Next steps\nedit it"
    quoting = f"## Findings\nthe template has\n\n{compaction.NOTES_HEADING}\n{tail}"
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    messages = compaction.apply_summary(head + _turns(2, 10), 2, 6, quoting) + _turns(4, 100)
    run = new_run(carry={"summary": quoting, "summary_notes": "", "notes": ""})
    assert run["summary"] == quoting and run["summary_notes"] == ""
    for kept in (run, {}):  # {}: a session from before the carry-over still keeps the quoted heading's tail
        text = _prompt(compaction.summary_request(messages, 2, len(messages) - 2, max_chars=100000,
                                                  known=kept.get("summary", ""), notes=kept.get("notes", ""),
                                                  known_notes=kept.get("summary_notes", "")))
        assert tail in text.partition("Conversation excerpt:")[0]
