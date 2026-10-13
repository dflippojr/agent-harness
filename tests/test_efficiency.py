"""Context-efficiency metrics (#159): composition, retries, correlation, cache, session payload."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from harness import compaction, efficiency
from harness.admin import PREFIX
from harness.api import create_app
from harness.efficiency import add_activity, canonical_args, compose, session_payload, turn_counts, turn_increments
from harness.llm import Completion, LLMError, _apply_chunk
from harness.manager import Manager
from harness.metrics import render
from test_daemon import Script, call, events, make_cfg, wait_status


def _ev(type_, seq, **data):
    return {"seq": seq, "type": type_, "data": data}


def _call(call_id, name, args, decision="allow"):
    return _ev("tool_call", 0, id=call_id, name=name, args=args, decision=decision)


def _result(call_id, name, ok, output="err", output_chars=None):
    data = {"id": call_id, "name": name, "ok": ok, "output": output}
    if output_chars is not None:
        data["output_chars"] = output_chars
    return _ev("tool_result", 0, **data)


def _assistant(n, call_id, name, args):
    import json
    return _ev("assistant", n, content="", tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }])


def test_canonical_args_sorts_keys_and_parses_strings():
    assert canonical_args({"b": 1, "a": 2}) == canonical_args('{"a": 2, "b": 1}')
    assert canonical_args({"b": 1, "a": 2}) != canonical_args({"a": 1, "b": 2})


def test_compose_bucket_order_and_schema_overhead():
    context = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "reasoning_content": "plan",
         "tool_calls": [{"id": "r1", "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}}]},
        {"role": "tool", "tool_call_id": "r1", "content": "file-bytes"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "s1", "function": {"name": "run_shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "s1", "content": "shell-out"},
        {"role": "user", "content": f"{compaction.STATE_TAG} notes"},
        {"role": "tool", "tool_call_id": "r2", "content": f"{compaction.RECEIPT_PREFIX} prior read"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "r2", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "d1", "content": "worker-prompt"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "d1", "function": {"name": "delegate", "arguments": "{}"}}]},
    ]
    # Re-order so r2's assistant precedes the receipt (origin lookup is by id).
    context = [
        context[0], context[1], context[2], context[3], context[4], context[5],
        context[6],
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "r2", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "r2", "content": f"{compaction.RECEIPT_PREFIX} prior read"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "d1", "function": {"name": "delegate", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "d1", "content": "worker-prompt"},
    ]
    raw = compose(context, schema_chars=300, chars_per_token=3.0, prompt_tokens=None)
    assert raw["estimated"] is True
    buckets = raw["buckets"]
    assert buckets["system_state"] > 0
    assert buckets["file_contents"] > 0
    assert buckets["tool_outputs"] > 0
    assert buckets["reasoning_other"] > 0
    # Receipts are tool_outputs, not file_contents; delegate results are ignored.
    receipt_only = compose([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "r2", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "r2", "content": f"{compaction.RECEIPT_PREFIX} prior read"},
    ], 0, 3.0, None)["buckets"]
    assert receipt_only["file_contents"] == 0
    assert receipt_only["tool_outputs"] > 0
    delegated = compose([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "d1", "function": {"name": "delegate", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "d1", "content": "x" * 90},
    ], 0, 3.0, None)["buckets"]
    assert delegated["tool_outputs"] == 0

    scaled = compose(context, schema_chars=300, chars_per_token=3.0, prompt_tokens=100)
    assert scaled["estimated"] is False
    assert sum(scaled["buckets"].values()) == 100


def test_retry_until_success_and_skip_policy_deny_interrupt():
    events = [
        _assistant(1, "a", "read_file", {"path": "missing.txt"}),
        _call("a", "read_file", {"path": "missing.txt"}),
        _result("a", "read_file", False, "Error: no such file: missing.txt"),
        _ev("turn_metrics", 10, turn=1, dead_end_retries=0, compaction_correlated_retries=efficiency.empty_correlated()),
        _assistant(2, "b", "read_file", {"path": "missing.txt"}),
        _call("b", "read_file", {"path": "missing.txt"}),
        _result("b", "read_file", False, "Error: no such file: missing.txt"),
        _ev("turn_metrics", 20, turn=2, dead_end_retries=1, compaction_correlated_retries=efficiency.empty_correlated()),
        _assistant(3, "c", "read_file", {"path": "missing.txt"}),
        _call("c", "read_file", {"path": "missing.txt"}),
        _result("c", "read_file", True, "ok"),
        _ev("turn_metrics", 30, turn=3, dead_end_retries=1, compaction_correlated_retries=efficiency.empty_correlated()),
        _assistant(4, "d", "read_file", {"path": "missing.txt"}),
        _call("d", "read_file", {"path": "missing.txt"}),
        _result("d", "read_file", False, "Error: no such file: missing.txt"),
    ]
    retries, correlated = turn_increments(events)
    assert retries == 0  # first failure after a success is not a retry
    assert correlated == efficiency.empty_correlated()

    block = [
        _assistant(1, "p", "run_shell", {"command": "rm -rf /"}),
        _call("p", "run_shell", {"command": "rm -rf /"}, decision="deny"),
        _result("p", "run_shell", False, "Error: blocked by policy (not allowed). Don't retry this."),
        _assistant(2, "p2", "run_shell", {"command": "rm -rf /"}),
        _call("p2", "run_shell", {"command": "rm -rf /"}, decision="deny"),
        _result("p2", "run_shell", False, "Error: blocked by policy (not allowed). Don't retry this."),
        _assistant(3, "n", "write_file", {"path": "a.txt", "content": "x"}),
        _call("n", "write_file", {"path": "a.txt", "content": "x"}, decision="ask"),
        _result("n", "write_file", False, "Error: the user denied this write_file call. Don't retry it."),
        _assistant(4, "n2", "write_file", {"path": "a.txt", "content": "x"}),
        _call("n2", "write_file", {"path": "a.txt", "content": "x"}, decision="ask"),
        _result("n2", "write_file", False, "Error: the user denied this write_file call."),
        _assistant(5, "i", "list_files", {}),
        _call("i", "list_files", {}),
        _result("i", "list_files", False,
                "Error: the daemon restarted while this tool call was running, so its effects are unknown."),
        _assistant(6, "i2", "list_files", {}),
        _call("i2", "list_files", {}),
        _result("i2", "list_files", False, "still missing"),
    ]
    retries, _ = turn_increments(block)
    assert retries == 0


def test_correlation_k_boundary_mask_only_and_shared_window():
    fail_args = {"path": "gone.txt"}
    prefix = [
        _assistant(1, "f1", "read_file", fail_args),
        _call("f1", "read_file", fail_args),
        _result("f1", "read_file", False, "Error: no such file"),
        _ev("turn_metrics", 5, turn=1, dead_end_retries=0,
            compaction_correlated_retries=efficiency.empty_correlated()),
    ]

    def repeat_at(turn, seq_base, after):
        cid = f"r{turn}"
        return after + [
            _assistant(seq_base, cid, "read_file", fail_args),
            _call(cid, "read_file", fail_args),
            _result(cid, "read_file", False, "Error: no such file"),
        ]

    # Compact, then five generates: the fifth retry counts; a sixth would not.
    elide = prefix + [_ev("compaction", 6, tier="elide", tokens_before=100, tokens_after=40)]
    at_five = elide
    for i in range(1, 6):
        at_five = repeat_at(i, 10 + i, at_five)
        if i < 5:
            at_five.append(_ev("turn_metrics", 20 + i, turn=1 + i, dead_end_retries=1 if i == 1 else 0,
                               compaction_correlated_retries=(
                                   {"elide": 1, "summary": 0, "round_reset": 0} if i == 1
                                   else efficiency.empty_correlated())))
    retries, correlated = turn_increments(at_five)
    assert retries == 1
    assert correlated["elide"] == 1

    at_six = at_five + [_ev("turn_metrics", 40, turn=6, dead_end_retries=1,
                            compaction_correlated_retries={"elide": 1, "summary": 0, "round_reset": 0})]
    at_six = repeat_at(6, 50, at_six)
    retries, correlated = turn_increments(at_six)
    assert retries == 1
    assert correlated["elide"] == 0

    mask_only = prefix + [_ev("compaction", 6, tier="mask", tokens_saved=10, characters_saved=80)]
    mask_only = repeat_at(2, 11, mask_only)
    retries, correlated = turn_increments(mask_only)
    assert retries == 1
    assert correlated == efficiency.empty_correlated()

    shared = prefix + [
        _ev("compaction", 6, tier="mask", tokens_saved=4, characters_saved=20),
        _ev("compaction", 7, tier="elide", tokens_before=90, tokens_after=40),
    ]
    shared = repeat_at(2, 11, shared)
    retries, correlated = turn_increments(shared)
    assert retries == 1
    assert correlated["elide"] == 1
    assert correlated["summary"] == 0


def test_correlation_survives_replay_like_a_process_restart():
    fail_args = {"path": "gone.txt"}
    first = [
        _assistant(1, "f1", "read_file", fail_args),
        _call("f1", "read_file", fail_args),
        _result("f1", "read_file", False, "Error: no such file"),
        _ev("turn_metrics", 5, turn=1, dead_end_retries=0,
            compaction_correlated_retries=efficiency.empty_correlated()),
        _ev("compaction", 6, tier="round_reset", tokens_before=80, tokens_after=20),
        _assistant(2, "x", "list_files", {}),
        _call("x", "list_files", {}),
        _result("x", "list_files", True, "."),
        _ev("turn_metrics", 10, turn=2, dead_end_retries=0,
            compaction_correlated_retries=efficiency.empty_correlated()),
    ]
    resumed = first + [
        _assistant(3, "r", "read_file", fail_args),
        _call("r", "read_file", fail_args),
        _result("r", "read_file", False, "Error: no such file"),
    ]
    retries, correlated = turn_increments(resumed)
    assert retries == 1
    assert correlated["round_reset"] == 1


def test_session_payload_nulls_without_new_fields():
    events = [
        _ev("tool_result", 1, id="a", name="run_shell", ok=True, output="hi"),
        _ev("assistant", 2, content="done", prompt_tokens=10, completion_tokens=2),
    ]
    payload = session_payload("s1", events)
    assert payload["session_id"] == "s1"
    assert payload["turns"] == []
    assert payload["aggregate"]["dead_end_retries"] is None
    assert payload["aggregate"]["compaction_correlated_retries"] == {
        "elide": None, "summary": None, "round_reset": None}
    assert payload["aggregate"]["largest_tool_output_chars"] is None
    assert payload["aggregate"]["largest_tool_output_by_tool"] == {}
    assert payload["aggregate"]["schema_tokens"] is None

    events.append(_result("b", "run_shell", False, "x" * 50, output_chars=50))
    events.append(_result("c", "read_file", True, "y" * 12, output_chars=12))
    events.append(_ev("turn_metrics", 9, turn=1, prompt_tokens=40, completion_tokens=3,
                      composition={"system_state": 10, "tool_outputs": 20, "file_contents": 5,
                                   "reasoning_other": 5},
                      estimated=False, cache_tokens=8, recomputed_tokens=32,
                      schema_tokens=30, schema_count=4, dead_end_retries=2,
                      compaction_correlated_retries={"elide": 1, "summary": 0, "round_reset": 0}))
    payload = session_payload("s1", events)
    assert payload["turns"][0]["cache_tokens"] == 8
    assert payload["turns"][0]["schema_tokens"] == 30
    assert payload["turns"][0]["schema_count"] == 4
    assert payload["aggregate"]["schema_tokens"] == 30
    assert payload["turns"][0]["estimated"] is False
    assert payload["aggregate"]["dead_end_retries"] == 2
    assert payload["aggregate"]["compaction_correlated_retries"]["elide"] == 1
    assert payload["aggregate"]["largest_tool_output_chars"] == 50
    assert payload["aggregate"]["largest_tool_output_by_tool"] == {"run_shell": 50, "read_file": 12}


def _read_artifact_args(digest):
    return {"artifact_id": digest}


def test_activity_counts_per_turn_and_repeats_across_turns():
    digest, other = "a" * 64, "b" * 64
    edit_args = {"path": "a.txt", "old_text": "one", "new_text": "two"}
    first = [
        _assistant(1, "r1", "read_artifact", _read_artifact_args(digest)),
        _call("r1", "read_artifact", _read_artifact_args(digest)),
        _result("r1", "read_artifact", True, "recovered"),
        _assistant(2, "e1", "edit_file", edit_args),
        _call("e1", "edit_file", edit_args),
        _result("e1", "edit_file", False, "Error: old_text must appear exactly once, found 2 occurrences"),
        _ev("compaction", 6, tier="elide", tokens_before=100, tokens_after=40, summary_averted=True),
    ]
    counts = turn_counts(first)
    assert counts["read_artifact_calls"] == 1
    assert counts["read_artifact_repeats"] == 0
    assert counts["edit_mismatches"] == 1
    assert counts["elide_only_compactions"] == 1
    assert counts["dead_end_retries"] == 0

    second = first + [_ev("turn_metrics", 10, turn=1, **counts)] + [
        _assistant(3, "r2", "read_artifact", _read_artifact_args(digest)),
        _call("r2", "read_artifact", _read_artifact_args(digest)),
        _result("r2", "read_artifact", True, "recovered again"),
        _assistant(4, "r3", "read_artifact", _read_artifact_args(other)),
        _call("r3", "read_artifact", _read_artifact_args(other)),
        _result("r3", "read_artifact", False, "Error: artifact not found"),
        _assistant(5, "r4", "read_artifact", _read_artifact_args(digest)),
        _call("r4", "read_artifact", _read_artifact_args(digest), decision="deny"),
        _result("r4", "read_artifact", False, "Error: blocked by policy (not allowed). Don't retry this."),
        _assistant(9, "r6", "read_artifact", {}),
        _call("r6", "read_artifact", {}),
        _result("r6", "read_artifact", False, "Error: bad arguments for read_artifact: 'artifact_id' is required"),
        _assistant(6, "e2", "edit_file", edit_args),
        _call("e2", "edit_file", edit_args),
        _result("e2", "edit_file", True, "edited"),
        _assistant(7, "e3", "edit_file", {"path": "missing.txt", "old_text": "x", "new_text": "y"}),
        _call("e3", "edit_file", {"path": "missing.txt", "old_text": "x", "new_text": "y"}),
        _result("e3", "edit_file", False, "Error: no such file: missing.txt"),
        _ev("compaction", 20, tier="elide", tokens_before=100, tokens_after=90),
    ]
    counts = turn_counts(second)
    # r2 and r3 are calls that ran; the denied r4 and the malformed r6 (refused before the tool) are not
    assert counts["read_artifact_calls"] == 2
    assert counts["read_artifact_repeats"] == 1  # r2 re-reads the digest recovered in the first turn
    assert counts["edit_mismatches"] == 0  # e2 succeeded; e3 failed for another reason
    assert counts["elide_only_compactions"] == 0  # the second elide left the context above summarize_at

    # A recovered digest that another turn re-reads is a repeat even after its own turn_metrics row.
    third = second + [_ev("turn_metrics", 30, turn=2, **counts), _assistant(8, "r5", "read_artifact",
                                                                               _read_artifact_args(digest)),
                      _call("r5", "read_artifact", _read_artifact_args(digest)),
                      _result("r5", "read_artifact", True, "again")]
    counts = turn_counts(third)
    assert counts["read_artifact_calls"] == 1
    assert counts["read_artifact_repeats"] == 1


def test_elide_only_needs_the_summary_averted_flag():
    events = [_ev("compaction", 1, tier="elide", tokens_before=100, tokens_after=90),
              _ev("compaction", 2, tier="summary", tokens_before=100, tokens_after=20, summary_averted=False)]
    assert turn_counts(events)["elide_only_compactions"] == 0


def test_add_activity_accumulates_into_totals_and_session_payload_sums_rows():
    totals = add_activity({"turns": 2, "prompt_tokens": 10}, {"read_artifact_calls": 2, "edit_mismatches": 1})
    assert totals == {"turns": 2, "prompt_tokens": 10, "read_artifact_calls": 2, "read_artifact_repeats": 0,
                      "edit_mismatches": 1, "elide_only_compactions": 0}
    assert add_activity(totals, {"read_artifact_calls": 1})["read_artifact_calls"] == 3

    row = {"dead_end_retries": 0, "compaction_correlated_retries": efficiency.empty_correlated()}
    assert session_payload("s1", [_ev("turn_metrics", 1, turn=1, **row)])["aggregate"]["edit_mismatches"] is None
    events = [
        _ev("turn_metrics", 1, turn=1, **row, read_artifact_calls=2, read_artifact_repeats=1, edit_mismatches=0,
            elide_only_compactions=1),
        _ev("turn_metrics", 2, turn=2, **row, read_artifact_calls=1, read_artifact_repeats=0, edit_mismatches=1,
            elide_only_compactions=0),
    ]
    aggregate = session_payload("s1", events)["aggregate"]
    assert aggregate["read_artifact_calls"] == 3
    assert aggregate["read_artifact_repeats"] == 1
    assert aggregate["edit_mismatches"] == 1
    assert aggregate["elide_only_compactions"] == 1


def test_apply_chunk_cache_from_first_progress_only():
    async def body():
        out = Completion()
        await _apply_chunk({"prompt_progress": {"processed": 10, "total": 100, "cache": -1}}, out, {}, None, None)
        assert out.cache_tokens is None
        await _apply_chunk({"prompt_progress": {"processed": 40, "total": 100, "cache": 25}}, out, {}, None, None)
        assert out.cache_tokens is None  # later chunks must not fill a missing first-chunk value

        out = Completion()
        await _apply_chunk({"prompt_progress": {"processed": 10, "total": 100, "cache": 7}}, out, {}, None, None)
        await _apply_chunk({"prompt_progress": {"processed": 40, "total": 100, "cache": 99}}, out, {}, None, None)
        assert out.cache_tokens == 7

        out = Completion()
        await _apply_chunk({"prompt_progress": {"processed": 50, "total": 100}}, out, {}, None, None)
        assert out.cache_tokens is None  # omitted cache is -1, never the processed fallback
    asyncio.run(body())


def test_schema_overhead_counts_schemas_and_estimates_tokens():
    tools = [{"type": "function", "function": {"name": "a" * 40}}, {"type": "function", "function": {"name": "b"}}]
    chars = len(json.dumps(tools))
    assert efficiency.schema_overhead(tools, 3.0) == {"schema_tokens": int(chars / 3.0), "schema_count": 2}
    assert efficiency.schema_overhead(tools, 0) == {"schema_tokens": int(chars / 3.0), "schema_count": 2}
    assert efficiency.schema_overhead([], 2.5) == {"schema_tokens": 0, "schema_count": 0}


def test_native_loop_records_schema_overhead_per_turn_and_in_totals(tmp_path):
    script = Script([
        Completion(tool_calls=[call("list_files", 0)], prompt_tokens=50, completion_tokens=3),
        Completion(content="done", prompt_tokens=60, completion_tokens=2),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("list")["id"], "done")
        expected = efficiency.schema_overhead(m.runner.tool_schemas(s, m.runner.workspace(s)), 3.0)
        rows = events(m, s["id"], "turn_metrics")
        assert len(rows) == 2
        assert expected["schema_count"] >= 12 and expected["schema_tokens"] > 0
        for row in rows:
            assert row["schema_count"] == expected["schema_count"]
            assert row["schema_tokens"] == expected["schema_tokens"]
        assert s["totals"]["schema_tokens"] == 2 * expected["schema_tokens"]
        payload = session_payload(s["id"], m.db.events(s["id"]))
        assert payload["aggregate"]["schema_tokens"] == 2 * expected["schema_tokens"]
        await m.stop()
    asyncio.run(body())


def test_native_loop_records_retries_output_chars_and_prometheus(tmp_path):
    big = "z" * 25000
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="big.txt", content=big)],
                   prompt_tokens=80, completion_tokens=5),
        Completion(tool_calls=[call("read_file", 1, path="nope.txt")], prompt_tokens=90, completion_tokens=5),
        Completion(tool_calls=[call("read_file", 2, path="nope.txt")], prompt_tokens=100, completion_tokens=5),
        Completion(tool_calls=[call("read_file", 3, path="big.txt")], prompt_tokens=110, completion_tokens=5),
        Completion(content="done", prompt_tokens=120, completion_tokens=4),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = await wait_status(m, m.create("try")["id"], "done")
        results = events(m, s["id"], "tool_result")
        assert all("output_chars" in row for row in results)
        read_ok = next(row for row in results if row["name"] == "read_file" and row["ok"])
        assert read_ok["output_chars"] > 20000
        assert len(read_ok["output"]) < read_ok["output_chars"]
        rows = events(m, s["id"], "turn_metrics")
        assert len(rows) == 5
        assert sum(row["dead_end_retries"] for row in rows) == 1
        assert rows[0]["cache_tokens"] == 0
        assert rows[0]["recomputed_tokens"] == 80
        assert rows[0]["composition"]["system_state"] is not None
        assert sum(rows[0]["composition"].values()) == 80
        text = render(m)
        assert "harness_dead_end_retries_total 1" in text
        assert 'harness_compaction_correlated_retries_total{tier="elide"} 0' in text
        assert 'harness_compaction_correlated_retries_total{tier="summary"} 0' in text
        assert 'harness_compaction_correlated_retries_total{tier="round_reset"} 0' in text
        assert "harness_prompt_cache_tokens_total{kind=\"cached\"} 0" in text
        assert "harness_prompt_cache_tokens_total{kind=\"recomputed\"}" in text
        payload = session_payload(s["id"], m.db.events(s["id"]))
        assert payload["aggregate"]["dead_end_retries"] == 1
        assert payload["aggregate"]["largest_tool_output_chars"] >= read_ok["output_chars"]
        await m.stop()
        return s["id"], m

    sid, m = asyncio.run(body())
    with TestClient(create_app(m)) as client:
        body = client.get(f"{PREFIX}/sessions/{sid}/metrics").json()
        assert body["session_id"] == sid
        assert body["aggregate"]["dead_end_retries"] == 1
        assert body["turns"][0]["composition"]["system_state"] is not None
        assert client.get(f"{PREFIX}/sessions/nosuchidxx/metrics").status_code == 404
        app = client.post("/keys", json={"name": "shop", "kind": "app",
                                         "scopes": ["sessions", "sessions:all"]}).json()
        refused = client.get(f"{PREFIX}/sessions/{sid}/metrics",
                             headers={"Authorization": f"Bearer {app['key']}"})
        assert refused.status_code == 403


def test_elide_correlates_retry_and_mask_does_not(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.elide_at = 0
    cfg.summarize_at = 99
    cfg.reset_at = 99
    script = Script([
        Completion(tool_calls=[call("read_file", 0, path="gone.txt")], prompt_tokens=40, completion_tokens=3),
        Completion(tool_calls=[call("read_file", 1, path="gone.txt")], prompt_tokens=50, completion_tokens=3),
        Completion(content="done", prompt_tokens=60, completion_tokens=2),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = await wait_status(m, m.create("retry after elide")["id"], "done")
        rows = events(m, s["id"], "turn_metrics")
        assert any(c["tier"] == "elide" for c in events(m, s["id"], "compaction"))
        assert sum(row["dead_end_retries"] for row in rows) == 1
        assert sum(row["compaction_correlated_retries"]["elide"] for row in rows) == 1
        text = render(m)
        assert 'harness_compaction_correlated_retries_total{tier="elide"} 1' in text
        await m.stop()
    asyncio.run(body())


def test_native_loop_counts_read_artifact_edit_mismatch_and_elide_only(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.elide_at = 0       # every pass elides...
    cfg.summarize_at = 99  # ...and that is always enough, so each one is elide-only
    cfg.reset_at = 99
    live = {}

    def recovered_digest() -> str:
        """The artifact of the big read_file result: the digest the model would copy from its receipt."""
        results = events(live["m"], live["sid"], "tool_result")
        return next(r["artifact_id"] for r in results if r["name"] == "read_file" and r["ok"])

    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="big.txt", content="z" * 25000),
                               call("write_file", 1, path="a.txt", content="one one")],
                   prompt_tokens=40, completion_tokens=3),
        Completion(tool_calls=[call("read_file", 2, path="big.txt"),
                               call("edit_file", 3, path="a.txt", old_text="one", new_text="two")],
                   prompt_tokens=50, completion_tokens=3),
        lambda _messages: Completion(tool_calls=[call("read_artifact", 4, artifact_id=recovered_digest())],
                                     prompt_tokens=60, completion_tokens=3),
        lambda _messages: Completion(tool_calls=[call("read_artifact", 5, artifact_id=recovered_digest())],
                                     prompt_tokens=70, completion_tokens=3),
        Completion(content="done", prompt_tokens=80, completion_tokens=2),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        sid = m.create("recover and edit")["id"]
        live.update(m=m, sid=sid)
        await wait_status(m, sid, "done")
        totals = m.db.get_session(sid)["totals"]
        assert totals["read_artifact_calls"] == 2
        assert totals["read_artifact_repeats"] == 1
        assert totals["edit_mismatches"] == 1
        elides = [c for c in events(m, sid, "compaction") if c["tier"] == "elide"]
        assert elides and all(c["summary_averted"] for c in elides)
        assert totals["elide_only_compactions"] == len(elides)
        rows = events(m, sid, "turn_metrics")
        assert sum(row["read_artifact_calls"] for row in rows) == 2
        assert sum(row["edit_mismatches"] for row in rows) == 1
        text = render(m)
        assert "harness_edit_mismatch_total 1" in text
        assert f"harness_compaction_elide_only_total {len(elides)}" in text
        aggregate = session_payload(sid, m.db.events(sid))["aggregate"]
        assert aggregate["read_artifact_calls"] == 2
        assert aggregate["read_artifact_repeats"] == 1
        assert aggregate["edit_mismatches"] == 1
        await m.stop()
        return sid, m

    sid, m = asyncio.run(body())
    with TestClient(create_app(m)) as client:
        session = client.get(f"{PREFIX}/sessions/{sid}").json()
        assert session["totals"]["read_artifact_calls"] == 2
        assert session["totals"]["edit_mismatches"] == 1


def test_pass_before_a_failed_model_call_still_reaches_totals(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.elide_at = 0
    cfg.summarize_at = 99
    cfg.reset_at = 99
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="a.txt", content="one one")],
                   prompt_tokens=40, completion_tokens=3),
        Completion(tool_calls=[call("edit_file", 1, path="a.txt", old_text="one", new_text="two")],
                   prompt_tokens=50, completion_tokens=3),
        LLMError("model server unreachable"),  # the third turn never commits, so its elide pass has no turn row
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        sid = m.create("fail after a pass")["id"]
        await wait_status(m, sid, "failed")
        elides = [c for c in events(m, sid, "compaction") if c["tier"] == "elide"]
        assert len(elides) == 3 and all(c["summary_averted"] for c in elides)
        rows = events(m, sid, "turn_metrics")
        # The last pass has no turn: its row carries the counts and no turn data.
        unturned = [row for row in rows if row.get("composition") is None]
        assert len(unturned) == 1 and unturned[0]["elide_only_compactions"] == 1
        totals = m.db.get_session(sid)["totals"]
        assert totals["elide_only_compactions"] == len(elides)
        assert totals["edit_mismatches"] == 1  # "one" occurs twice in "one one"
        assert sum(row["elide_only_compactions"] for row in rows) == len(elides)
        # A session from before the counts has no such totals keys; the summary still reports them as zero.
        legacy = m.summary({**m.db.get_session(sid), "totals": {"turns": 1}})
        assert legacy["totals"]["read_artifact_calls"] == 0 and legacy["totals"]["turns"] == 1
        await m.stop()
        return sid, m

    sid, m = asyncio.run(body())
    with TestClient(create_app(m)) as client:
        assert client.get(f"{PREFIX}/sessions/{sid}").json()["totals"]["elide_only_compactions"] == 3


def test_metrics_survive_manager_restart(tmp_path):
    from harness.db import Database
    cfg = make_cfg(tmp_path)
    script = Script([
        Completion(tool_calls=[call("read_file", 0, path="gone.txt")], prompt_tokens=40, completion_tokens=3),
        Completion(tool_calls=[call("read_file", 1, path="gone.txt")], prompt_tokens=50, completion_tokens=3),
        Completion(content="done", prompt_tokens=60, completion_tokens=2),
    ])

    async def first():
        db = Database(cfg.db_path)
        m = Manager(cfg, db=db, chat=script)
        await m.start()
        s = await wait_status(m, m.create("persist")["id"], "done")
        sid = s["id"]
        await m.stop()
        return sid, m.db

    async def second(sid, db):
        m = Manager(cfg, db=db, chat=script)
        await m.start()
        assert "harness_dead_end_retries_total 1" in render(m)
        payload = session_payload(sid, db.events(sid))
        assert payload["aggregate"]["dead_end_retries"] == 1
        await m.stop()

    sid, db = asyncio.run(first())
    asyncio.run(second(sid, db))


def test_admin_catalog_lists_session_metrics():
    from harness.admin import ADMIN_PATHS
    assert "/sessions/{ref}/metrics" in ADMIN_PATHS


def test_read_artifact_with_unparseable_arguments_is_not_a_call():
    events = [
        _assistant(1, "r1", "read_artifact", []),
        _call("r1", "read_artifact", []),
        _result("r1", "read_artifact", False, "Error: tool arguments were not a valid JSON object "
                                              "(arguments must be a JSON object)."),
    ]
    assert turn_counts(events)["read_artifact_calls"] == 0  # refused before the tool ran


def test_read_artifact_the_session_does_not_have_is_not_a_call():
    digest = "a" * 64
    events = [
        _assistant(1, "r1", "read_artifact", _read_artifact_args(digest)),
        _call("r1", "read_artifact", _read_artifact_args(digest)),
        _result("r1", "read_artifact", False, "Error: unknown tool 'read_artifact'. Available: read_file."),
    ]
    assert turn_counts(events)["read_artifact_calls"] == 0  # disabled by policy, so refused before it ran


def test_recovery_that_reads_like_a_rejection_still_counts():
    digest = "a" * 64
    events = []
    for n, call_id in enumerate(("r1", "r2")):
        events += [_assistant(n, call_id, "read_artifact", _read_artifact_args(digest)),
                   _call(call_id, "read_artifact", _read_artifact_args(digest)),
                   _result(call_id, "read_artifact", True, "Error: bad arguments for run_shell: an old log line")]
    counts = turn_counts(events)
    assert counts["read_artifact_calls"] == 2
    assert counts["read_artifact_repeats"] == 1


def test_hosted_run_end_adds_no_native_retry_metrics(tmp_path):
    cfg = make_cfg(tmp_path)
    script = Script([Completion(content="done", prompt_tokens=10, completion_tokens=2)])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        sid = m.create("hosted retries")["id"]
        await wait_status(m, sid, "done")
        before = len(events(m, sid, "turn_metrics"))
        m.db.update_session(sid, backend="claude")
        args = {"command": "false"}

        def hosted_retries() -> None:  # a hosted Bash call that failed, then the same call again
            for call_id in ("b1", "b2"):
                m.runner.bus.emit(sid, "tool_call", {"id": call_id, "name": "Bash", "args": args, "decision": "allow"})
                m.runner.bus.emit(sid, "tool_result", {"id": call_id, "name": "Bash", "ok": False, "output": "exit 1"})
        m.db.for_session(sid).write(hosted_retries)
        assert efficiency.turn_counts(m.db.events(sid))["dead_end_retries"] == 1
        m.runner._emit_turn_metrics(sid, final=True)
        assert len(events(m, sid, "turn_metrics")) == before
        assert session_payload(sid, m.db.events(sid))["aggregate"]["dead_end_retries"] in (None, 0)
        assert "harness_dead_end_retries_total 1" not in render(m)
        await m.stop()

    asyncio.run(body())
