"""Daemon tests with a scripted model. Only test_sandbox_* needs Docker; nothing needs the GPU."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import compaction
from harness.config import Config, ModelConfig, Project, SandboxConfig
from harness.db import Database
from harness.llm import Completion, LLMError
from harness.manager import Manager
from harness.policy import ALLOW, ASK, Policy
from harness.runner import INTERRUPTED
from harness.scheduler import GpuScheduler


def make_cfg(tmp: Path, context_tokens: int = 65536, rules: list | None = None) -> Config:
    return Config(
        host="127.0.0.1", port=0, data_dir=tmp / "data", repos_dir=tmp / "repos", default_model="fake",
        models={"fake": ModelConfig(name="fake", base_url="http://unused", context_tokens=context_tokens)},
        sandbox=SandboxConfig(image="agent-harness-sandbox:py312", network="harness-test-sbx",
                              egress_network="harness-test-egress"),
        projects={"scratch": Project(name="scratch"), "guarded": Project(name="guarded", rules=rules or [])},
    )


def call(name: str, idx: int = 0, **args) -> dict:
    return {"id": f"c{idx}-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    """Returns step N for the Nth assistant turn in the context, so it replays correctly after a restart."""

    def __init__(self, steps):
        self.steps = steps
        self.requests: list[list[dict]] = []

    async def __call__(self, model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0):
        self.requests.append(messages)
        if tools is None:
            return Completion(content="SUMMARY: did things", prompt_tokens=100, completion_tokens=10)
        n = sum(1 for m in messages if m["role"] == "assistant")
        step = self.steps[min(n, len(self.steps) - 1)]
        if callable(step):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        if on_delta and step.content:
            await on_delta("content", step.content)
        return step


async def wait_status(m: Manager, sid: str, *statuses: str, timeout: float = 10) -> dict:
    for _ in range(int(timeout / 0.02)):
        s = m.db.get_session(sid)
        if s["status"] in statuses:
            return s
        await asyncio.sleep(0.02)
    raise AssertionError(f"session stayed {m.db.get_session(sid)['status']}, wanted {statuses}")


def events(m: Manager, sid: str, type_: str) -> list[dict]:
    return [e["data"] for e in m.db.events(sid) if e["type"] == type_]


# policy
@pytest.mark.parametrize("args,expected", [
    ({"command": "pytest -q"}, ALLOW),
    ({"command": "git push origin main"}, ASK),
    ({"command": "pip install requests", "network": True}, ASK),
    ({"command": "rm -rf __pycache__ .pytest_cache"}, ALLOW),
    ({"command": "rm -f /tmp/out.txt"}, ALLOW),
    ({"command": "rm -rf src"}, ASK),
    ({"command": "rm notes.md && ls"}, ASK),
    ({"command": "cd sub && rm x"}, ASK),
    ({"command": "find . -name '*.log' -delete"}, ASK),
    ({"command": "git reset --hard HEAD~1"}, ASK),
    ({"command": "echo rm is a word"}, ALLOW),
])
def test_policy_shell(args, expected):
    assert Policy().decide("run_shell", args).action == expected


def test_policy_project_rules_and_clone():
    p = Policy([{"tool": ["write_file", "edit_file"], "path": "categories/health/**", "action": "ask",
                 "reason": "sensitive"}])
    assert p.decide("write_file", {"path": "categories/health/memory.md", "content": ""}).action == ASK
    assert p.decide("write_file", {"path": "/workspace/categories/health/x.md", "content": ""}).action == ASK
    assert p.decide("write_file", {"path": "categories/sport/memory.md", "content": ""}).action == ALLOW
    assert p.decide("git_clone", {"url": "https://github.com/a/b"}).action == ALLOW
    assert p.decide("git_clone", {"url": "local:demo"}).action == ALLOW
    assert p.decide("git_clone", {"url": "https://example.com/a/b"}).action == ASK


# compaction
def test_split_keeps_tool_results_with_their_call():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(10):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call("read_file", i, path="x")]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}-read_file", "content": "x" * 1000})
    start, end = compaction.split_for_summary(msgs, keep_chars=2500)
    assert start == 2 and msgs[end]["role"] == "assistant"
    new = compaction.apply_summary(msgs, start, end, "notes")
    assert new[0]["content"] == "sys" and new[1]["content"] == "task"
    assert new[2]["content"].startswith(compaction.SUMMARY_TAG)
    # The newest turn's results are never summarized, even when they alone exceed the budget.
    start3, end3 = compaction.split_for_summary(msgs, keep_chars=10)
    assert end3 == len(msgs) - 2
    elided, _ = compaction.elide(msgs, keep_last=0, max_chars=10)
    assert elided[-1]["content"] == msgs[-1]["content"] and elided[-3]["content"] != msgs[-3]["content"]
    # A second summary folds the first one in instead of stacking.
    start2, end2 = compaction.split_for_summary(new, keep_chars=1200)
    again = compaction.apply_summary(new, start2, end2, "notes2")
    assert sum(1 for m in again if (m.get("content") or "").startswith(compaction.SUMMARY_TAG)) == 1


def test_scheduler_fifo():
    async def body():
        order = []
        s = GpuScheduler()
        await s.acquire("a")

        async def worker(sid):
            await s.acquire(sid)
            order.append(sid)
            await asyncio.sleep(0.01)
            s.release(sid)

        tasks = [asyncio.create_task(worker(x)) for x in "bcd"]
        await asyncio.sleep(0.01)
        assert s.positions() == {"a": 0, "b": 1, "c": 2, "d": 3}
        tasks[1].cancel()  # c leaves the queue
        await asyncio.sleep(0.01)
        s.release("a")
        await asyncio.gather(*tasks, return_exceptions=True)
        assert order == ["b", "d"] and s.holder is None
    asyncio.run(body())


# runner flows
def test_basic_run_writes_file_and_finishes(tmp_path):
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="hello.txt", content="hi\n")], prompt_tokens=500),
        Completion(tool_calls=[call("read_file", 1, path="hello.txt")], prompt_tokens=600),
        Completion(content="Wrote hello.txt.", prompt_tokens=700, completion_tokens=5),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create("write hello")
        s = await wait_status(m, s["id"], "done")
        await asyncio.gather(*m.tasks.values())  # transcript is written as the run wraps up
        assert s["answer"] == "Wrote hello.txt." and s["stop_reason"] == "final_message"
        assert (Path(s["workspace"]) / "hello.txt").read_text() == "hi\n"
        results = events(m, s["id"], "tool_result")
        assert [r["ok"] for r in results] == [True, True] and "1\thi" in results[1]["output"]
        text = (m.cfg.transcripts_dir / f"{s['id']}.md").read_text(encoding="utf-8")
        assert "Wrote hello.txt." in text and "write_file" in text
        await m.stop()
    asyncio.run(body())


def test_approval_survives_restart(tmp_path):
    rules = [{"tool": "write_file", "path": "secret/*", "action": "ask", "reason": "sensitive"}]
    cfg = make_cfg(tmp_path, rules=rules)
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="secret/a.txt", content="x")]),
        Completion(content="done writing"),
    ])

    async def first():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("write secret", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")
        pending = m.db.pending_approvals(s["id"])
        assert len(pending) == 1 and "+x" in pending[0]["detail"]
        assert m.scheduler.holder is None  # the GPU is free while waiting
        await m.stop()
        m.db.close()
        return s["id"]

    async def second(sid):
        m = Manager(cfg, chat=script)
        await m.start()
        await asyncio.sleep(0.1)
        assert m.db.get_session(sid)["status"] == "waiting_approval"
        m.decide(sid, None, approve=True)
        s = await wait_status(m, sid, "done")
        assert (Path(s["workspace"]) / "secret" / "a.txt").read_text() == "x"
        assert events(m, sid, "resumed")
        await m.stop()

    sid = asyncio.run(first())
    asyncio.run(second(sid))


def test_denied_call_is_reported_to_model(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    script = Script([
        Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
        lambda msgs: Completion(content="ok, skipped: " + msgs[-1]["content"]),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("try", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")
        m.decide(s["id"], None, approve=False, note="not now")
        s = await wait_status(m, s["id"], "done")
        assert "denied" in s["answer"] and "not now" in s["answer"]
        assert not (Path(s["workspace"]) / "a.txt").exists()
        await m.stop()
    asyncio.run(body())


def test_interrupted_tool_call_is_marked_after_restart(tmp_path):
    cfg = make_cfg(tmp_path)
    script = Script([
        Completion(tool_calls=[call("list_files", 0)]),
        lambda msgs: Completion(content="saw: " + msgs[-1]["content"][:40]),
    ])

    async def body():
        db = Database(cfg.db_path)
        m = Manager(cfg, db=db, chat=script)
        s = m.create("list")  # task created but the loop never gets to run
        m.tasks[s["id"]].cancel()
        await asyncio.sleep(0.05)
        # Simulate a crash mid-tool-call: assistant message stored, tool marked as executing, no result.
        s = db.get_session(s["id"])
        ctx = s["context"] + [{"role": "assistant", "content": "", "tool_calls": [call("list_files", 0)]}]
        run = s["run"] | {"executing": {"id": "c0-list_files", "name": "list_files"}}
        db.update_session(s["id"], context=ctx, run=run, status="running")
        m2 = Manager(cfg, db=db, chat=script)
        await m2.start()
        s = await wait_status(m2, s["id"], "done")
        assert s["answer"].startswith("saw: " + INTERRUPTED[:30])
        await m2.stop()
    asyncio.run(body())


def test_retries_invalid_calls_and_bad_args(tmp_path):
    attempts = {"n": 0}

    def flaky(msgs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return LLMError("HTTP 500: failed to parse tool call", retryable=True)
        return Completion(tool_calls=[
            {"id": "bad", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}},
            call("read_file", 1, path="x.txt", bogus=1),
            call("nope", 2),
        ])

    script = Script([flaky, Completion(content="gave up")])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create("go")
        s = await wait_status(m, s["id"], "done")
        outputs = [r["output"] for r in events(m, s["id"], "tool_result")]
        assert "not a valid JSON" in outputs[0] and "unknown argument" in outputs[1] and "unknown tool" in outputs[2]
        assert s["run"]["invalid_tool_calls"] == 4 and events(m, s["id"], "llm_retry")
        await m.stop()
    asyncio.run(body())


def test_cancel_and_follow_up_message(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask"}])
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a", content="x")]),
                     Completion(content="continued")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = m.create("x", project="guarded")
        await wait_status(m, s["id"], "waiting_approval")
        s = await m.cancel(s["id"])
        assert s["status"] == "cancelled" and s["context"][-1]["content"].startswith("Not run")
        assert m.db.pending_approvals(s["id"]) == []
        await m.send(s["id"], "never mind, just say hi")
        s = await wait_status(m, s["id"], "done")
        assert s["answer"] == "continued"
        await m.stop()
    asyncio.run(body())


def test_budget_and_finish_tool(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.max_turns = 2
    script = Script([Completion(tool_calls=[call("list_files", i)]) for i in range(5)])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        s = await wait_status(m, m.create("loop forever")["id"], "done")
        assert s["stop_reason"] == "budget_turns" and s["run"]["turns"] == 2
        await m.stop()

        m = Manager(make_cfg(tmp_path / "b"), chat=Script([
            Completion(tool_calls=[call("finish", 0, answer="42"), call("list_files", 1)])]))
        await m.start()
        s = await wait_status(m, m.create("answer")["id"], "done")
        assert s["answer"] == "42" and s["stop_reason"] == "finished"
        assert s["context"][-1]["content"].startswith("Not run")
        await m.stop()
    asyncio.run(body())


def test_compaction_summarizes_long_context(tmp_path):
    cfg = make_cfg(tmp_path, context_tokens=8000)
    big = "line of output\n" * 400  # ~6K chars per result

    def step(msgs):
        n = sum(1 for m in msgs if m["role"] == "assistant")
        if n < 8:
            return Completion(tool_calls=[call("run_fake", n)], prompt_tokens=sum(
                compaction.message_chars(m) for m in msgs) // 3)
        return Completion(content="final", prompt_tokens=1000)

    script = Script([step])

    async def body():
        m = Manager(cfg, chat=script)
        # Stand-in tool so the test doesn't need Docker: unknown tools return an error of known size.
        orig = m.runner._record_result

        def padded(sid, c, name, output, ok, seconds=0.0):
            orig(sid, c, name, output + big, ok, seconds)
        m.runner._record_result = padded
        await m.start()
        s = await wait_status(m, m.create("long task")["id"], "done", timeout=20)
        comp = events(m, s["id"], "compaction")
        assert any(c["tier"] == "summary" for c in comp)
        assert any((x.get("content") or "").startswith(compaction.SUMMARY_TAG) for x in s["context"])
        assert s["context"][1]["content"] == "long task"
        await m.stop()
    asyncio.run(body())


# Docker
docker_ok = shutil.which("docker") and subprocess.run(
    ["docker", "image", "inspect", "agent-harness-sandbox:py312"], capture_output=True).returncode == 0


@pytest.mark.skipif(not docker_ok, reason="needs Docker and the sandbox image")
def test_sandbox_shell_local_clone_and_network_gate(tmp_path):
    repos = tmp_path / "repos"
    src = repos / "demo"
    src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "a.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                   check=True)
    script = Script([
        Completion(tool_calls=[call("git_clone", 0, url="local:demo")]),
        Completion(tool_calls=[call("run_shell", 1, command="cd demo && python a.py && git log --oneline | wc -l")]),
        Completion(tool_calls=[call("run_shell", 2, command="getent hosts github.com || echo no-dns")]),
        Completion(tool_calls=[call("run_shell", 3, command="getent hosts github.com >/dev/null && echo online",
                                    network=True)]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        s = m.create("clone and run")
        s = await wait_status(m, s["id"], "waiting_approval", "done", "failed", timeout=120)
        assert s["status"] == "waiting_approval", s["stop_reason"]
        m.decide(s["id"], None, approve=True)
        s = await wait_status(m, s["id"], "done", "failed", timeout=120)
        outputs = [r["output"] for r in events(m, s["id"], "tool_result")]
        assert outputs[0].startswith("cloned")
        assert "hi" in outputs[1] and outputs[1].rstrip().endswith("1")
        assert "no-dns" in outputs[2]
        assert "online" in outputs[3]
        await asyncio.gather(*m.tasks.values())  # "done" is committed before the container is stopped
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", f"harness-{s['id']}"],
                               capture_output=True, text=True).stdout.strip()
        assert state == "false"  # stopped at the end of the run
        await m.stop()
        subprocess.run(["docker", "rm", "-f", f"harness-{s['id']}"], capture_output=True)
    try:
        asyncio.run(body())
    finally:
        subprocess.run(["docker", "network", "rm", "harness-test-sbx", "harness-test-egress"], capture_output=True)
