"""Prefix-cache study tooling (#774, docs/prefix-cache-study.md): the canary's --capture-prompts and --keep-metrics
debug options, and bakeoff.prefix_diff's offline diff. Nothing needs the GPU."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakeoff import canary as canary_cli, prefix_diff
from harness import compaction, config as config_mod, fake_instance, fake_model, planning
from harness.llm import Completion, LLMError
from harness.manager import Manager

SYSTEM = {"role": "system", "content": "You are the agent."}
TOOLS = [{"type": "function", "function": {"name": "read_file"}}, {"type": "function", "function": {"name": "finish"}}]


def run(coro):
    return asyncio.run(coro)


def row(seq: int, messages: list[dict], session: str = "s1", tools=TOOLS, **extra) -> dict:
    return {"seq": seq, "session": session, "messages": messages, "tools": tools, **extra}


# --capture-prompts
def test_capture_writes_each_call_in_order_with_the_messages_as_sent(tmp_path):
    async def chat(model, messages, tools=None, **kwargs):
        messages.append({"role": "assistant", "content": "changed after the call"})  # the runner keeps editing
        return Completion(content="ok", reasoning="think", prompt_tokens=10, cache_tokens=4, prompt_ms=12.5,
                          tool_calls=[{"id": "c1"}])

    path = tmp_path / "out" / "p.jsonl"
    capture = canary_cli.PromptCapture(path, chat=chat, holder=lambda: "sid1")
    for text in ("one", "two"):
        run(capture(SimpleNamespace(name="m"), [SYSTEM, {"role": "user", "content": text}], TOOLS, max_tokens=5,
                    extra={"chat_template_kwargs": {"enable_thinking": False}}))
    rows = prefix_diff.read_jsonl(path)
    assert [r["seq"] for r in rows] == [1, 2]
    assert [r["messages"][-1]["content"] for r in rows] == ["one", "two"]  # the snapshot is from before the call
    assert rows[0]["session"] == "sid1" and rows[0]["model"] == "m" and rows[0]["tools"] == TOOLS
    assert (rows[0]["prompt_tokens"], rows[0]["cache_tokens"], rows[0]["prompt_ms"]) == (10, 4, 12.5)
    assert rows[0]["reply"] == {"content": "ok", "reasoning": "think", "tool_calls": [{"id": "c1"}]}
    assert rows[0]["extra"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert rows[0]["caller"] and all(isinstance(name, str) for name in rows[0]["caller"])


def test_a_failed_call_is_recorded_and_still_raises(tmp_path):
    async def chat(*args, **kwargs):
        raise LLMError("HTTP 500: boom", retryable=True)

    path = tmp_path / "p.jsonl"
    path.write_text("stale\n", encoding="utf-8")
    capture = canary_cli.PromptCapture(path, chat=chat)
    with pytest.raises(LLMError):
        run(capture(SimpleNamespace(name="m"), [SYSTEM]))
    [only] = prefix_diff.read_jsonl(path)  # a new capture starts an empty file
    assert only["error"].startswith("LLMError: HTTP 500") and "prompt_tokens" not in only


# --keep-metrics
def test_turn_metrics_by_session_keeps_only_turn_metrics_rows():
    events = {"a": [{"type": "turn_metrics", "data": {"turn": 1, "cache_tokens": 0}},
                    {"type": "tool_result", "data": {}},
                    {"type": "turn_metrics", "data": {"turn": 2, "cache_tokens": 90}}],
              "b": []}
    db = SimpleNamespace(
        sessions_with_status=lambda *statuses: [{"id": i, "title": f"canary {i}", "status": "done"} for i in "ab"]
        if statuses == canary_cli.DONE else [],
        events=lambda sid: events[sid])
    out = canary_cli.turn_metrics_by_session(db)
    assert [s["session"] for s in out] == ["a", "b"]
    assert out[0]["turns"] == [{"turn": 1, "cache_tokens": 0}, {"turn": 2, "cache_tokens": 90}]
    assert out[1] == {"session": "b", "title": "canary b", "status": "done", "turns": []}


def test_main_passes_the_debug_options_through_and_leaves_them_off_by_default(monkeypatch):
    seen = []

    async def standalone(tasks, repeats, fixture, **kwargs):
        seen.append((tasks, repeats, kwargs))

    monkeypatch.setattr(canary_cli, "run_standalone", standalone)
    canary_cli.main(["--tasks", "log_correlation", "--repeats", "1"])
    canary_cli.main(["--tasks", "a,b", "--keep-metrics", "m.json", "--capture-prompts", "p.jsonl"])
    assert "keep_metrics" not in seen[0][2] and "capture_prompts" not in seen[0][2]
    assert seen[1][0] == ["a", "b"]
    assert seen[1][2]["keep_metrics"] == Path("m.json") and seen[1][2]["capture_prompts"] == Path("p.jsonl")


# bakeoff.prefix_diff
def test_first_message_diff():
    user = {"role": "user", "content": "task"}
    assert prefix_diff.first_message_diff([SYSTEM, user], [SYSTEM, user, {"role": "assistant"}]) is None
    changed = {"role": "user", "content": "tasks"}
    index, offset = prefix_diff.first_message_diff([SYSTEM, user], [SYSTEM, changed])
    assert index == 1 and json.dumps(user)[:offset] == json.dumps(changed)[:offset]
    assert json.dumps(user)[offset] != json.dumps(changed)[offset]
    assert prefix_diff.first_message_diff([SYSTEM, user], [SYSTEM]) == (1, 0)


@pytest.mark.parametrize("prev, cur, expected", [
    (row(1, [SYSTEM]), row(2, [SYSTEM], session="s2"), "other session"),
    (row(1, [SYSTEM]), row(2, [SYSTEM], tools=TOOLS[::-1]), "tool-schema order"),
    (row(1, [SYSTEM]), row(2, [SYSTEM], tools=TOOLS[:1]), "tool schemas changed"),
    (row(1, [{"role": "system", "content": "x {base_branch}"}]), row(2, [{"role": "system", "content": "x main"}]),
     "system prompt: {base_branch} filled"),
    (row(1, [SYSTEM]), row(2, [{"role": "system", "content": SYSTEM["content"] + "\n\n<repo-map>\nm\n</repo-map>"}]),
     "system prompt: repo map rewritten"),
    (row(1, [{"role": "system", "content": "Today is 2026-10-29."}]),
     row(2, [{"role": "system", "content": "Today is 2026-10-30."}]), "date or time"),
    (row(1, [SYSTEM, {"role": "tool", "content": "x" * 3000}]),
     row(2, [SYSTEM, {"role": "tool", "content": compaction.RECEIPT_PREFIX + " tool=read_file"}]),
     "masking: observation receipt"),
    (row(1, [SYSTEM, {"role": "tool", "content": "a\nb\nc"}]),
     row(2, [SYSTEM, {"role": "tool", "content": "a\n[... 1 lines omitted: older command output ...]\nc"}]),
     "compaction: trim or elide"),
    (row(1, [SYSTEM, {"role": "assistant", "content": "a", "reasoning_content": "think"}]),
     row(2, [SYSTEM, {"role": "assistant", "content": "a"}]), "compaction: elide dropped reasoning"),
    (row(1, [SYSTEM, {"role": "user", "content": "go"}, {"role": "assistant", "content": "x" * 900}]),
     row(2, [SYSTEM, {"role": "user", "content": "go"}, {"role": "user", "content": compaction.STATE_TAG + " {}"},
             {"role": "user", "content": "Continue."}]), "compaction: round reset"),
    (row(1, [SYSTEM, {"role": "user", "content": "go"}, {"role": "assistant", "content": "x" * 900}]),
     row(2, [SYSTEM, {"role": "user", "content": compaction.SUMMARY_TAG + " done so far"}]), "compaction: summary"),
    (row(1, [SYSTEM, {"role": "tool", "content": "out" + planning.block("Plan:\n1. read")}]),
     row(2, [SYSTEM, {"role": "tool", "content": "out"}, {"role": "assistant", "content": "next"}]),
     "injected state: plan re-injection"),
    (row(1, [SYSTEM, {"role": "assistant", "content": "a", "tool_calls": [{"id": "1"}]}]),
     row(2, [SYSTEM, {"role": "assistant", "content": "a", "tool_calls": [{"id": "2"}]}]),
     "assistant turn re-serialized"),
    (row(1, [SYSTEM, {"role": "user", "content": "go"}]),
     row(2, [{"role": "system", "content": compaction.SUMMARY_SYSTEM}, {"role": "user", "content": "go"}]),
     "compaction summary request"),
])
def test_classify(prev, cur, expected):
    assert prefix_diff.classify(prev, cur, prefix_diff.first_message_diff(prev["messages"], cur["messages"])) \
        == expected


def test_pairs_and_summary_price_a_break_at_the_measured_prefill_rate():
    user = {"role": "user", "content": "task"}
    tool = {"role": "tool", "content": "y" * 3000}
    masked = {"role": "tool", "content": compaction.RECEIPT_PREFIX}
    rows = [row(1, [SYSTEM, user], prompt_tokens=1000, cache_tokens=0, prompt_ms=9000, tokens=list(range(1000))),
            row(2, [SYSTEM, user, tool], prompt_tokens=2000, cache_tokens=1000, prompt_ms=1000,
                tokens=list(range(2000))),
            row(3, [SYSTEM, user, masked], prompt_tokens=1200, cache_tokens=400, prompt_ms=800,
                tokens=list(range(1100)) + [-1] * 100)]
    [first, second] = prefix_diff.pairs(rows)
    assert first["class"] == "" and first["lost_tokens"] == 0 and first["broke"] is False
    assert second["class"] == "masking: observation receipt" and second["message"] == 2
    assert (second["shared_tokens"], second["lost_tokens"], second["server_shortfall"]) == (1100, 800, 700)
    out = prefix_diff.summary(rows)
    assert out["ms_per_token"] == 1.0  # (1000 + 800) ms over (1000 + 800) tokens; the cold first request left out
    assert out["sessions"]["s1"] == {"requests": 3, "prompt_tokens": 4200, "cache_tokens": 1400, "reuse": 0.333}
    assert out["breaks"] == {"masking: observation receipt": {"count": 1, "lost_tokens": 800, "seconds": 0.8}}


def test_new_messages_are_found_by_identity_not_position():
    user = {"role": "user", "content": "go"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "x" * 300}
    newest = {"role": "tool", "tool_call_id": "c2", "content": "y" * 300}
    reply = {"role": "assistant", "content": "next"}
    long_history = [SYSTEM, user, reply, result, reply, {"role": "tool", "tool_call_id": "c0", "content": "z"}, reply]
    # A reset that shrinks the history still has to read the result it keeps, and its state note.
    state = {"role": "user", "content": compaction.STATE_TAG + " {}"}
    after_reset = row(2, [SYSTEM, user, state, reply, newest])
    assert prefix_diff.new_messages(row(1, long_history), after_reset) == [state, newest]
    assert prefix_diff.appended_tokens(row(1, long_history), after_reset) == round(len(json.dumps([state, newest])) / 3)
    # A masked result is the same call: rewriting it adds nothing.
    masked = {"role": "tool", "tool_call_id": "c1", "content": compaction.RECEIPT_PREFIX}
    assert prefix_diff.new_messages(row(1, [SYSTEM, user, reply, result]), row(2, [SYSTEM, user, reply, masked])) == []
    # A plan appended to the user's own message is not a new message once it is gone.
    planned = {"role": "user", "content": "go" + planning.block("Plan:\n1. read")}
    assert prefix_diff.new_messages(row(1, [SYSTEM, planned]), row(2, [SYSTEM, user])) == []


def test_a_request_after_a_summary_is_compared_with_the_last_agent_request():
    user = {"role": "user", "content": "go"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "r" * 3000}
    reply = {"role": "assistant", "content": "next"}
    rows = [row(1, [SYSTEM, user], prompt_tokens=100, cache_tokens=0),
            row(2, [{"role": "system", "content": compaction.SUMMARY_SYSTEM}, {"role": "user", "content": "notes"}],
                prompt_tokens=300, cache_tokens=0),
            row(3, [SYSTEM, user, reply, result], prompt_tokens=1500, cache_tokens=50)]
    summary_call, after = prefix_diff.pairs(rows)
    assert summary_call["class"] == after["class"] == "compaction summary request"
    assert summary_call["lost_tokens"] == 300  # the whole summary prompt is what compacting cost
    assert after["lost_tokens"] == 1500 - 50 - round(len(json.dumps([result])) / 3)  # its new result is not


def test_a_failed_call_does_not_hide_a_break_from_its_retry():
    user = {"role": "user", "content": "go"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "x" * 3000}
    masked = {"role": "tool", "tool_call_id": "c1", "content": compaction.RECEIPT_PREFIX}
    rows = [row(1, [SYSTEM, user, result], prompt_tokens=1000, cache_tokens=0, prompt_ms=1000),
            row(2, [SYSTEM, user, masked], error="LLMError: connection reset"),
            row(3, [SYSTEM, user, masked], prompt_tokens=800, cache_tokens=100, prompt_ms=700)]
    [only] = prefix_diff.pairs(rows)
    assert only["seq"] == 3 and only["class"] == "masking: observation receipt" and only["lost_tokens"] == 700
    out = prefix_diff.summary(rows)
    assert out["failed_requests"] == 1 and out["sessions"]["s1"]["requests"] == 2
    assert out["breaks"]["masking: observation receipt"]["lost_tokens"] == 700


def test_report_reads_a_capture_and_writes_json(tmp_path, capsys):
    path = tmp_path / "p.jsonl"
    rows = [row(1, [SYSTEM], prompt_tokens=10, cache_tokens=0), row(2, [SYSTEM, {"role": "user", "content": "x"}],
                                                                    prompt_tokens=12, cache_tokens=10)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert prefix_diff.main(["report", str(path), "--json", str(tmp_path / "r.json")]) == 0
    assert "seq=2 prompt_tokens=12 cache_tokens=10" in capsys.readouterr().out
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["sessions"]["s1"]["reuse"] == 0.455  # 10 of 22


def test_render_asks_the_server_for_the_template_and_tokens(monkeypatch):
    posted = []

    class Client:
        def __init__(self, base_url, timeout):
            self.base_url = base_url

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json):
            posted.append((path, json))
            body = {"prompt": "<rendered>"} if path == "/apply-template" else {"tokens": [1, 2, 3]}
            return SimpleNamespace(raise_for_status=lambda: SimpleNamespace(json=lambda: body))

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    reply = {"role": "assistant", "content": "calling"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "output"}
    options = {"chat_template_kwargs": {"enable_thinking": False}}
    first, second = prefix_diff.render([row(1, [SYSTEM], extra=options), row(2, [SYSTEM, {"role": "user"}], error="x"),
                                        row(3, [SYSTEM, reply, result])], "http://127.0.0.1:1")
    assert first["prompt"] == "<rendered>" and first["tokens"] == [1, 2, 3] and "new_tokens" not in first
    assert second["new_tokens"] == 3 + prefix_diff.MESSAGE_OVERHEAD  # the tool result; the reply was generated
    assert second["seq"] == 3  # the failed call is left out
    assert posted[0] == ("/apply-template", {"messages": [SYSTEM], "tools": TOOLS, **options})
    assert posted[1][1] == {"content": "<rendered>", "add_special": False, "parse_special": True}
    assert {"content": "output", "add_special": False, "parse_special": True} in [body for _, body in posted]
    assert not [body for _, body in posted if body.get("content") == "calling"]


# decision 2's harness-side pass: the real runner with the scripted model
STUDY_SCRIPT = """
context_tokens: 16384
scripts:
  - name: prefix-study
    match: "prefix study"
    turns:
      - text: "Writing."
        tool_calls: [{name: write_file, args: {path: data.txt, content: "%s"}}]
      - text: "Saving the plan."
        tool_calls: [{name: update_state, args: {goal: "Report", plan: "1. read data.txt", next_step: "read"}}]
      - text: "Reading."
        tool_calls: [{name: read_file, args: {path: data.txt}}]
      - text: "Listing."
        tool_calls: [{name: list_files, args: {path: "."}}]
      - text: "Listing again."
        tool_calls: [{name: list_files, args: {path: "."}}]
      - text: "A long pass."
        filler_chars: 40000
        tool_calls: [{name: list_files, args: {path: "."}}]
      - text: "Done."
""" % ("line of data " * 400)


@pytest.mark.parametrize("reinject", [False, True])
def test_a_fake_session_breaks_the_prefix_only_where_compaction_or_the_plan_does(monkeypatch, tmp_path, reinject):
    """Within one session the system prompt and the tool schemas never change between consecutive requests; the
    breaks are masking, the round reset and, with planning.reinject on, the appended plan (#641)."""
    from harness import sandbox

    def no_docker(*args, **kwargs):
        raise AssertionError("fake mode must not call Docker")

    monkeypatch.setattr(sandbox, "run_cmd", no_docker)
    config_dir, data_dir = fake_instance.write_instance(tmp_path / "instance", 0)
    script = tmp_path / "fixtures.yaml"
    script.write_text(STUDY_SCRIPT, encoding="utf-8")
    monkeypatch.setenv(fake_model.ENV, str(script))
    cfg = config_mod.load(config_dir, data_dir)
    cfg.planning = config_mod.PlanningConfig(reinject=reinject)
    m = Manager(cfg)
    path = tmp_path / "p.jsonl"
    m.runner.chat = canary_cli.PromptCapture(path, chat=m.runner.chat, holder=lambda: m.scheduler.holder)

    async def go():
        await m.start(maintenance=False)
        try:
            s = m.create("Run the prefix study.", project=fake_instance.PROJECT)
            for _ in range(600):
                if m.db.get_session(s["id"])["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            return m.db.get_session(s["id"])
        finally:
            await m.stop()

    try:
        final = run(go())
    finally:
        m.db.close()
    assert final["status"] == "done", final
    rows = prefix_diff.read_jsonl(path)
    agent = [r for r in rows if r["messages"][0]["content"] != compaction.SUMMARY_SYSTEM]
    assert len({json.dumps(r["tools"]) for r in agent}) == 1
    assert len({r["messages"][0]["content"] for r in agent}) == 1
    classes = {p["class"] for p in prefix_diff.pairs(rows)}
    assert "masking: observation receipt" in classes and "compaction: round reset" in classes
    assert ("injected state: plan re-injection" in classes) is reinject
    assert classes <= {"", "masking: observation receipt", "compaction: round reset",
                       "injected state: plan re-injection"}, classes
