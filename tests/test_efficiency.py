"""Context-efficiency metrics (#159): composition, retries, correlation, cache, session payload."""

from __future__ import annotations

import asyncio

from harness import compaction, efficiency
from harness.efficiency import CORRELATION_K, canonical_args, compose, session_payload, turn_increments
from harness.llm import Completion, _apply_chunk


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

    events.append(_result("b", "run_shell", False, "x" * 50, output_chars=50))
    events.append(_result("c", "read_file", True, "y" * 12, output_chars=12))
    events.append(_ev("turn_metrics", 9, turn=1, prompt_tokens=40, completion_tokens=3,
                      composition={"system_state": 10, "tool_outputs": 20, "file_contents": 5,
                                   "reasoning_other": 5},
                      estimated=False, cache_tokens=8, recomputed_tokens=32,
                      dead_end_retries=2,
                      compaction_correlated_retries={"elide": 1, "summary": 0, "round_reset": 0}))
    payload = session_payload("s1", events)
    assert payload["turns"][0]["cache_tokens"] == 8
    assert payload["turns"][0]["estimated"] is False
    assert payload["aggregate"]["dead_end_retries"] == 2
    assert payload["aggregate"]["compaction_correlated_retries"]["elide"] == 1
    assert payload["aggregate"]["largest_tool_output_chars"] == 50
    assert payload["aggregate"]["largest_tool_output_by_tool"] == {"run_shell": 50, "read_file": 12}


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
