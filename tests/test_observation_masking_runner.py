"""Runner-level observation masking: _maybe_compact against artifacts stored in the DB, and read_artifact."""

from __future__ import annotations

import asyncio
import hashlib

from harness import compaction
from harness.llm import Completion
from harness.manager import Manager
from test_daemon import Script, call, events, make_cfg, wait_status

BIG = "0123456789" * 6000  # 60,000 characters: over the 20,000-character read cap


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _session(m: Manager, prompt: str = "task") -> dict:
    await m.start()
    return await wait_status(m, m.create(prompt)["id"], "done")


def _seed_result(m: Manager, sid: str, idx: int, output: str, name: str = "read_file") -> str:
    """Append one assistant tool call plus its recorded result, the way a real turn does."""
    c = call(name, idx, path="big.txt")
    s = m.db.get_session(sid)
    m.db.update_session(sid, context=s["context"] + [{"role": "assistant", "content": None, "tool_calls": [c]}])
    m.runner._record_result(sid, c, name, output[:20000], ok=True, artifact_content=output)
    return c["id"]


def _finish_turn(m: Manager, sid: str) -> None:
    s = m.db.get_session(sid)
    m.db.update_session(sid, context=s["context"] + [{"role": "assistant", "content": "used it"}])


def test_masking_loads_the_full_stored_output_not_the_capped_read(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        call_id = _seed_result(m, sid, 1, BIG)
        _finish_turn(m, sid)
        await m.runner._maybe_compact(m.db.get_session(sid))

        tool = next(x for x in m.db.get_session(sid)["context"] if x.get("tool_call_id") == call_id)
        assert f"characters={len(BIG)}" in tool["content"] and _digest(BIG) in tool["content"]
        assert m.db.full_artifact(sid, _digest(BIG)) == BIG
        rest, truncated = m.db.read_artifact(sid, _digest(BIG), 40000, len(BIG))
        assert rest == BIG[40000:] and not truncated
        assert [e["characters_saved"] for e in events(m, sid, "compaction") if e["tier"] == "mask"] == [
            20000 - len(tool["content"])]
        await m.stop()
    asyncio.run(body())


def test_masking_is_idempotent_and_emits_once(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        _seed_result(m, sid, 1, BIG)
        _finish_turn(m, sid)
        await m.runner._maybe_compact(m.db.get_session(sid))
        masked_context = m.db.get_session(sid)["context"]
        for _ in range(3):
            await m.runner._maybe_compact(m.db.get_session(sid))
        assert m.db.get_session(sid)["context"] == masked_context
        assert len([e for e in events(m, sid, "compaction") if e["tier"] == "mask"]) == 1

        # A new result still gets masked later, and only that one produces a second event.
        _seed_result(m, sid, 2, "y" * 3000)
        _finish_turn(m, sid)
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert len([e for e in events(m, sid, "compaction") if e["tier"] == "mask"]) == 2
        assert all(x["content"].startswith(compaction.RECEIPT_PREFIX)
                   for x in m.db.get_session(sid)["context"] if x["role"] == "tool")
        await m.stop()
    asyncio.run(body())


def test_masking_skips_fresh_and_small_results(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        _seed_result(m, sid, 1, "small")
        _seed_result(m, sid, 2, BIG)  # newest turn: the model has not read it yet
        before = m.db.get_session(sid)["context"]
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert m.db.get_session(sid)["context"] == before
        assert not [e for e in events(m, sid, "compaction") if e["tier"] == "mask"]
        await m.stop()
    asyncio.run(body())


def test_read_artifact_tool_ranges_missing_and_other_session(tmp_path):
    async def read(m, sid, idx, **args):
        c = call("read_artifact", idx, **args)
        await m.runner._resolve_call(m.db.get_session(sid), c, [], {}, 10**9)
        ok = [e["data"]["ok"] for e in m.db.events(sid) if e["type"] == "tool_result" and e["data"]["id"] == c["id"]]
        # The context holds the full text the model sees; the event's copy is shortened for the UI.
        text = next(x["content"] for x in m.db.get_session(sid)["context"] if x.get("tool_call_id") == c["id"])
        return text, ok[-1]

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        other = m.create("other")["id"]
        await wait_status(m, other, "done")
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        m.db.put_artifact(other, _digest("secret"), "secret")

        out, ok = await read(m, sid, 1, artifact_id=digest)  # no range: capped, flagged as truncated
        assert ok and out.startswith(BIG[:20000]) and "[truncated; request a smaller range" in out
        assert len(out) < 20100
        out, ok = await read(m, sid, 2, artifact_id=digest, start=40000, end=40010)
        assert (out, ok) == (BIG[40000:40010], True)
        out, ok = await read(m, sid, 3, artifact_id=digest, start=59990)  # open end: to the end of the artifact
        assert (out, ok) == (BIG[59990:], True)
        out, ok = await read(m, sid, 4, artifact_id=digest, start=0, end=30000)  # range wider than the cap
        assert ok and out.startswith(BIG[:20000]) and "truncated" in out

        out, ok = await read(m, sid, 5, artifact_id="f" * 64)
        assert (out, ok) == ("Error: artifact not found", False)
        out, ok = await read(m, sid, 6, artifact_id=_digest("secret"))  # stored under another session
        assert (out, ok) == ("Error: artifact not found", False)

        for idx, bad in enumerate(({"artifact_id": "short"}, {"artifact_id": "G" * 64}, {"artifact_id": 5},
                                   {"artifact_id": digest, "start": -1}, {"artifact_id": digest, "start": True},
                                   {"artifact_id": digest, "start": 10, "end": 5},
                                   {"artifact_id": digest, "end": "x"}, {}), start=10):
            out, ok = await read(m, sid, idx, **bad)
            assert not ok and out.startswith("Error:"), bad
        await m.stop()
    asyncio.run(body())
