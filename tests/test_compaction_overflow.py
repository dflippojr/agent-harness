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
    text = _prompt(compaction.summary_request(messages, start, end, max_chars=budget))
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
    calls = {"failed": 0}

    def step(msgs):
        n = sum(1 for m in msgs if m["role"] == "assistant")
        if n < 4:
            return Completion(tool_calls=[call("run_fake", n)])
        if not calls["failed"]:
            calls["failed"] += 1
            return LLMError(OVERFLOW)
        return Completion(content="final")

    script = Script([step])

    async def body():
        m = Manager(_overflow_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("long task")["id"], "done")
        assert s["answer"] == "final"
        overflow = [c for c in events(m, s["id"], "compaction") if c.get("reason") == "overflow"]
        assert len(overflow) == 1 and overflow[0]["tier"] == "summary"
        assert sum(1 for r in script.requests if r[0]["content"] == compaction.SUMMARY_SYSTEM) == 1
        retry = script.requests[-1]
        assert retry[2]["content"].startswith(compaction.SUMMARY_TAG)   # the retry got the compacted context
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


def test_prior_notes_that_use_up_the_budget_leave_no_excerpt():
    messages = _summarized()
    budget = len(compaction.PRIOR_NOTES.format(prior=FIRST)) - 10   # the notes alone are over it
    text = _prompt(compaction.summary_request(messages, 2, len(messages) - 2, max_chars=budget))
    assert FIRST in text
    assert text.endswith("Conversation excerpt:\n[earliest part omitted]\n")
    assert "result 0" not in text
