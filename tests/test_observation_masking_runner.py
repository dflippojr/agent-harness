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


# read_artifact must get every guarantee an ordinary tool result gets between _resolve_calls and the context.

def _ctx_text(m: Manager, sid: str, call_id: str) -> str:
    return next(x["content"] for x in m.db.get_session(sid)["context"] if x.get("tool_call_id") == call_id)


def _result_event(m: Manager, sid: str, call_id: str) -> dict:
    return next(e["data"] for e in m.db.events(sid) if e["type"] == "tool_result" and e["data"]["id"] == call_id)


def test_parallel_read_artifact_calls_share_the_turn_budget(tmp_path):
    async def body():
        # 5,000-token window x 3 chars/token x 0.35 = 5,250 characters for the whole turn.
        m = Manager(make_cfg(tmp_path, context_tokens=5000), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        calls = [call("read_artifact", i, artifact_id=digest) for i in range(1, 4)]
        assert await m.runner._resolve_calls(s, calls) is False
        first, second, third = (_ctx_text(m, sid, c["id"]) for c in calls)
        assert first.startswith(BIG[:5250]) and "output cut at 5250 characters" in first
        assert len(first) < 5250 + 300
        # The later calls see what the earlier ones used, so they fall to the 2,000-character floor.
        for later in (second, third):
            assert later.startswith(BIG[:2000]) and "output cut at 2000 characters" in later
            assert len(later) < 2000 + 300
        await m.stop()
    asyncio.run(body())


def test_read_artifact_output_counts_as_used_and_refreshes_the_turn(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        c = call("read_artifact", 1, artifact_id=digest, start=0, end=100)
        assert await m.runner._resolve_call(s, c, [], {}, 10**9) == (None, 100)
        c = call("read_artifact", 2, artifact_id="f" * 64)  # errors are counted too, like any tool's
        done, used = await m.runner._resolve_call(m.db.get_session(sid), c, [], {}, 10**9)
        assert (done, used) == (None, len("Error: artifact not found"))
        await m.stop()
    asyncio.run(body())


def test_read_artifact_full_chunk_is_recorded_whole_and_event_is_capped(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        c = call("read_artifact", 1, artifact_id=digest)
        await m.runner._resolve_calls(s, [c])
        text = _ctx_text(m, sid, c["id"])
        assert text.startswith(BIG[:20000]) and "[truncated; request a smaller range" in text
        event = _result_event(m, sid, c["id"])
        assert event["ok"] is True and len(event["output"]) <= 20000 + 100  # events keep a bounded copy
        await m.stop()
    asyncio.run(body())


def test_read_artifact_goes_through_policy_and_emits_tool_call(tmp_path):
    from harness.policy import Policy

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        allowed = call("read_artifact", 1, artifact_id=digest, start=0, end=5)
        await m.runner._resolve_calls(s, [allowed])
        decision = next(e["data"] for e in m.db.events(sid) if e["type"] == "tool_call" and e["data"]["id"] == allowed["id"])
        assert decision["name"] == "read_artifact" and decision["decision"] == "allow"
        assert _ctx_text(m, sid, allowed["id"]) == BIG[:5]

        m.runner.policy = lambda _s: Policy([{"tool": "read_artifact", "action": "deny", "reason": "not today"}])
        denied = call("read_artifact", 2, artifact_id=digest, start=0, end=5)
        await m.runner._resolve_calls(m.db.get_session(sid), [denied])
        assert "blocked by policy (not today)" in _ctx_text(m, sid, denied["id"])
        assert _result_event(m, sid, denied["id"])["ok"] is False
        await m.stop()
    asyncio.run(body())


def test_read_artifact_bad_arguments_count_as_invalid_tool_calls(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        before = m.db.get_session(sid)["run"].get("invalid_tool_calls", 0)
        bad = [call("read_artifact", 1), call("read_artifact", 2, artifact_id="f" * 64, extra=1),
               call("read_artifact", 3, artifact_id="f" * 64, start="abc")]
        await m.runner._resolve_calls(s, bad)
        for c in bad:
            assert _ctx_text(m, sid, c["id"]).startswith("Error: bad arguments for read_artifact:")
        assert m.db.get_session(sid)["run"]["invalid_tool_calls"] == before + 3
        await m.stop()
    asyncio.run(body())


def test_read_artifact_is_only_available_where_it_is_offered(tmp_path):
    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest("abc")
        m.db.put_artifact(sid, digest, "abc")
        m.db.update_session(sid, kind="chat")  # chat sessions are not offered the tool, so they cannot call it
        c = call("read_artifact", 1, artifact_id=digest)
        await m.runner._resolve_calls(m.db.get_session(sid), [c])
        assert "unknown tool 'read_artifact'" in _ctx_text(m, sid, c["id"])
        await m.stop()
    asyncio.run(body())


def test_read_artifact_result_is_grounded_and_never_masked_again(tmp_path):
    from harness import grounding

    async def body():
        m = Manager(make_cfg(tmp_path), chat=Script([Completion(content="done")]))
        s = await _session(m)
        sid = s["id"]
        digest = _digest(BIG)
        m.db.put_artifact(sid, digest, BIG)
        c = call("read_artifact", 1, artifact_id=digest, start=100, end=9100)  # 9,000 chars: over the mask threshold
        m.db.update_session(sid, context=m.db.get_session(sid)["context"]
                            + [{"role": "assistant", "content": None, "tool_calls": [c]}])
        await m.runner._resolve_calls(m.db.get_session(sid), [c])
        recovered = _ctx_text(m, sid, c["id"])
        assert recovered == BIG[100:9100]
        assert _result_event(m, sid, c["id"])["artifact_id"] is None  # no artifact of its own, so no receipt loop
        assert any(recovered in src for src in grounding.session_sources(
            m.db.get_session(sid)["context"], m.db.events(sid)))

        _finish_turn(m, sid)
        await m.runner._maybe_compact(m.db.get_session(sid))
        assert _ctx_text(m, sid, c["id"]) == recovered  # still verbatim after a later turn
        assert not any(e["type"] == "compaction" and e["data"].get("tier") == "mask" for e in m.db.events(sid))
        await m.stop()
    asyncio.run(body())
