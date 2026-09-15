"""Phase 8c: quotes in final answers must come from something the agent read."""

from __future__ import annotations

import asyncio

from harness.grounding import ungrounded_quotes
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, call, events, make_cfg, wait_status

PAGE = "SearXNG is free software released under the GNU Affero General Public License, version 3 or later."


def test_ungrounded_quotes_matching():
    sources = [PAGE, "d f f = 2048 and the model uses **six layers** in the encoder stack"]
    assert ungrounded_quotes('It says "released under the GNU Affero General Public License".', sources) == []
    # spacing, case and Markdown inside the quote don't matter; ellipses need every part
    assert ungrounded_quotes('"the model uses six layers in the encoder stack"', sources) == []
    assert ungrounded_quotes('"SearXNG is free software … General Public License, version 3"', sources) == []
    assert ungrounded_quotes('"SearXNG is free software … licensed under the MIT license terms"', sources) == [
        "SearXNG is free software … licensed under the MIT license terms"]
    # short quotes (names, terms) aren't checked
    assert ungrounded_quotes('Licensed as "AGPL-3.0".', sources) == []
    assert ungrounded_quotes("no quotes at all", []) == []


def test_final_answer_quote_gets_one_fix(tmp_path):
    script = Script([
        Completion(content='The license is AGPL. The README says "SearXNG is licensed under the strong AGPL terms".'),
        lambda msgs: Completion(content="Fixed: " + ("yes" if "don't appear word for word" in msgs[-1]["content"]
                                                     else "no") + ' "released under the GNU Affero General Public License"'),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create(f"What license? Source text: {PAGE}")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert s["answer"].startswith("Fixed: yes")
        assert events(m, s["id"], "quote_check")[0]["quotes"] == ["SearXNG is licensed under the strong AGPL terms"]
        assert events(m, s["id"], "ungrounded_quotes") == []
        assert "ungrounded_quotes" not in events(m, s["id"], "run_finished")[-1]
        await m.stop()
    asyncio.run(body())


def test_finish_tool_quote_is_flagged_after_second_try(tmp_path):
    made_up = "our tests show a 40 percent speedup on every workload"
    script = Script([
        Completion(tool_calls=[call("finish", 0, answer=f'Done. The docs say "{made_up}".')]),
        Completion(tool_calls=[call("finish", 1, answer=f'Done. The docs say "{made_up}".')]),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create("summarize the docs")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        results = events(m, s["id"], "tool_result")
        assert results[0]["ok"] is False and "Not finished yet" in results[0]["output"]
        assert results[1]["ok"] is True
        assert len(events(m, s["id"], "quote_check")) == 1
        assert events(m, s["id"], "ungrounded_quotes")[0]["quotes"] == [made_up]
        assert events(m, s["id"], "run_finished")[-1]["ungrounded_quotes"] == [made_up]
        assert m.db.get_session(s["id"])["run"]["ungrounded_quotes"] == [made_up]
        await m.stop()
    asyncio.run(body())


def test_quote_check_can_be_turned_off(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.web.quote_check = False
    script = Script([Completion(content='It says "this sentence appears nowhere in any source text".')])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("anything")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())
        assert events(m, s["id"], "quote_check") == [] and events(m, s["id"], "ungrounded_quotes") == []
        await m.stop()
    asyncio.run(body())
