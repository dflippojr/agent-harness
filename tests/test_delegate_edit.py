"""Delegated edits (#157): delegate_edit proposes through a fresh model call, apply_delegated_edit writes it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness import delegate_edit
from harness.fileops import FileOps, ToolError
from harness.llm import Completion, LLMError
from harness.manager import Manager
from harness.policy import Policy
from test_daemon import Script, call, make_cfg, wait_status

SOURCE = "def greet():\n    return 'hello'\n\n\ndef part():\n    return 1\n"
OTHER = "VALUE = 1\n"


def _reply(edits, summary: str = "renamed things", **kw) -> Completion:
    return Completion(content=json.dumps({"summary": summary, "edits": edits}), prompt_tokens=kw.get("p", 50),
                      completion_tokens=kw.get("c", 7), finish_reason=kw.get("finish", "stop"))


class Delegate:
    """Stands in for the model on delegate calls (tools=None) and records what each one was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[list[dict]] = []
        self.kwargs: list[dict] = []

    async def __call__(self, model, messages, tools, on_delta=None, max_tokens=None, extra=None, timeout=0,
                       on_progress=None):
        if tools is not None:  # the worker's own turn
            return Completion(content="done")
        self.requests.append(messages)
        self.kwargs.append({"max_tokens": max_tokens, "extra": extra})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def _session(m: Manager, files: dict[str, str] | None = None) -> tuple[dict, Path]:
    await m.start()
    s = await wait_status(m, m.create("task")["id"], "done")
    root = Path(s["workspace"])
    for name, text in (files if files is not None else {"app.py": SOURCE, "other.py": OTHER}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8", newline="")
    return s, root


async def _run(m: Manager, sid: str, *calls: dict) -> None:
    s = m.db.get_session(sid)
    context = s["context"] + [{"role": "assistant", "content": None, "tool_calls": list(calls)}]
    m.db.update_session(sid, context=context)
    await m.runner._resolve_calls(m.db.get_session(sid), list(calls))


def _result(m: Manager, sid: str, c: dict) -> str:
    return next(x["content"] for x in m.db.get_session(sid)["context"] if x.get("tool_call_id") == c["id"])


def _payload(m: Manager, sid: str, c: dict) -> dict:
    text = _result(m, sid, c)
    return json.loads(text.removeprefix("Error: "))


def _manager(tmp_path, delegate: Delegate, **kw) -> Manager:
    m = Manager(make_cfg(tmp_path, **kw), chat=Script([Completion(content="done")]))
    m.runner.chat = delegate
    return m


# the module, without a session

def test_parse_reply_rejects_malformed_empty_partial_and_foreign_paths():
    texts = {"a.py": "x = 1\n"}
    for content, finish, message in [
        ("", "stop", "returned nothing"),
        ("I would change x.", "stop", "not JSON"),
        ('{"summary": "s", "edits": [{"path": "a.py", "old_text": "x"', "length", "cut off"),
        ('{"summary": "nothing to do", "edits": []}', "stop", "no edits: nothing to do"),
        ('{"edits": [{"path": "a.py", "old_text": "x"}]}', "stop", "not a {path, old_text, new_text}"),
        ('{"edits": [{"path": "../b.py", "old_text": "x", "new_text": "y"}]}', "stop", "not one of the listed"),
        ('{"edits": [{"path": "a.py", "old_text": "", "new_text": "y"}]}', "stop", "empty old_text"),
    ]:
        with pytest.raises(ToolError, match=message.replace("{", r"\{").replace("}", r"\}")):
            delegate_edit.parse_reply(content, finish, texts)
    fenced = '```json\n{"summary": "s", "edits": [{"path": "./a.py", "old_text": "x", "new_text": "y"}]}\n```'
    assert delegate_edit.parse_reply(fenced, "stop", texts) == ("s", [{"path": "a.py", "old_text": "x",
                                                                       "new_text": "y"}])


def test_apply_to_texts_is_all_or_nothing_and_rejects_ambiguous_and_overlapping():
    texts = {"a.py": "one two three two\n"}
    with pytest.raises(ToolError, match="exactly once, found 2"):
        delegate_edit.apply_to_texts(texts, [{"path": "a.py", "old_text": "one", "new_text": "1"},
                                             {"path": "a.py", "old_text": "two", "new_text": "2"}])
    with pytest.raises(ToolError, match="overlap"):
        delegate_edit.apply_to_texts(texts, [{"path": "a.py", "old_text": "one two", "new_text": "x"},
                                             {"path": "a.py", "old_text": "two three", "new_text": "y"}])
    # Edits are applied against the original text, in file order whatever order they were listed in.
    assert delegate_edit.apply_to_texts(texts, [{"path": "a.py", "old_text": "three", "new_text": "3"},
                                                {"path": "a.py", "old_text": "one", "new_text": "1"}]) == {
        "a.py": "1 two 3 two\n"}


def test_read_inputs_containment_binary_nul_missing_and_caps(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bin.dat").write_bytes(b"\xff\xfe\x00garbage")
    (root / "nul.txt").write_text("a\x00b", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    files = FileOps(root, 65536)
    assert delegate_edit.read_inputs(files, ["ok.py"], 1000) == {"ok.py": "x = 1\n"}
    for paths, message in [(["bin.dat"], "binary file: bin.dat is not UTF-8"),
                           (["nul.txt"], "NUL byte"),
                           (["missing.py"], "no such file"),
                           (["../secret.txt"], "escapes the workspace"),
                           ([], "at least one file")]:
        with pytest.raises(ToolError, match=message):
            delegate_edit.read_inputs(files, paths, 1000)
    with pytest.raises(ToolError, match="at most 3"):
        delegate_edit.read_inputs(files, ["ok.py"], 3)
    (root / "huge.py").write_text("y" * (files.read_chars + 1), encoding="utf-8")
    with pytest.raises(ToolError, match=f"may be at most {files.read_chars}"):
        delegate_edit.read_inputs(files, ["huge.py"], 10**9)
    try:
        (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    except OSError:
        return  # no symlink privilege on this Windows account
    with pytest.raises(ToolError, match="escapes the workspace"):
        delegate_edit.read_inputs(files, ["link.txt"], 1000)


def test_crlf_files_match_and_keep_their_text(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    files = FileOps(root, 65536)
    texts = delegate_edit.read_inputs(files, ["w.py"], 1000)
    edits = [{"path": "w.py", "old_text": "b = 2\n", "new_text": "b = 3\n"}]
    _, proposal = delegate_edit.new_proposal(texts, edits, "k", "s")
    assert delegate_edit.apply(files, proposal) == "applied w.py"
    assert (root / "w.py").read_text(encoding="utf-8") == "a = 1\nb = 3\n"


# through the runner

def test_propose_then_apply_keeps_snippets_out_of_the_worker_context(tmp_path):
    old, new = "return 'hello'", "return 'hello, world'"
    delegate = Delegate(_reply([{"path": "app.py", "old_text": old, "new_text": new},
                                {"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]))

    async def body():
        m = _manager(tmp_path, delegate)
        s, root = await _session(m)
        sid = s["id"]
        propose = call("delegate_edit", 1, paths=["app.py", "other.py"], instruction="greet the world")
        await _run(m, sid, propose)
        result = _payload(m, sid, propose)
        assert result["status"] == "proposed" and result["error"] is None
        assert "app.py: 1 edit, +1 -1 lines" in result["summary"] and "renamed things" in result["summary"]
        assert (root / "app.py").read_text(encoding="utf-8") == SOURCE  # nothing written yet

        # The delegate got a fresh context: the files and the instruction, nothing of the worker's conversation.
        sent = delegate.requests[0]
        assert [x["role"] for x in sent] == ["system", "user"]
        assert SOURCE in sent[1]["content"] and "greet the world" in sent[1]["content"]
        assert "task" not in sent[1]["content"].split("Instruction:")[0]
        assert delegate.kwargs[0]["max_tokens"] == delegate_edit.DELEGATE_MAX_TOKENS

        apply = call("apply_delegated_edit", 2, patch_id=result["patch_id"])
        await _run(m, sid, apply)
        assert _payload(m, sid, apply)["status"] == "applied"
        assert (root / "app.py").read_text(encoding="utf-8") == SOURCE.replace(old, new)
        assert (root / "other.py").read_text(encoding="utf-8") == "VALUE = 2\n"

        run = m.db.get_session(sid)["run"]
        assert run["delegate_proposals"] == {} and run["delegate_edits"] == {}
        assert {"app.py", "other.py"} <= set(run["files_touched"])
        worker = json.dumps(m.db.get_session(sid)["context"])
        for snippet in (old, new, "VALUE = 2", "def part"):
            assert snippet not in worker
        await m.stop()
    asyncio.run(body())


def test_atomic_multi_edit_failure_writes_nothing(tmp_path):
    delegate = Delegate(_reply([{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                                {"path": "app.py", "old_text": "    return", "new_text": "    yield"}]))

    async def body():
        m = _manager(tmp_path, delegate)
        s, root = await _session(m)
        sid = s["id"]
        c = call("delegate_edit", 1, paths=["app.py", "other.py"], instruction="x")
        await _run(m, sid, c)
        result = _payload(m, sid, c)
        assert result["status"] == "error" and "exactly once, found 2" in result["error"]
        assert (root / "other.py").read_text(encoding="utf-8") == OTHER
        assert m.db.get_session(sid)["run"].get("delegate_proposals") is None
        await m.stop()
    asyncio.run(body())


def test_stale_file_between_propose_and_apply_writes_nothing_and_keeps_retries(tmp_path):
    delegate = Delegate(_reply([{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                                {"path": "app.py", "old_text": "return 1", "new_text": "return 2"}]))

    async def body():
        m = _manager(tmp_path, delegate)
        s, root = await _session(m)
        sid = s["id"]
        propose = call("delegate_edit", 1, paths=["app.py", "other.py"], instruction="x")
        await _run(m, sid, propose)
        patch_id = _payload(m, sid, propose)["patch_id"]
        (root / "app.py").write_text(SOURCE + "# changed\n", encoding="utf-8")
        attempts = dict(m.db.get_session(sid)["run"]["delegate_edits"])

        apply = call("apply_delegated_edit", 2, patch_id=patch_id)
        await _run(m, sid, apply)
        result = _payload(m, sid, apply)
        assert result["status"] == "error" and "stale proposal: app.py changed" in result["error"]
        assert (root / "other.py").read_text(encoding="utf-8") == OTHER  # the unchanged file wasn't written either
        assert m.db.get_session(sid)["run"]["delegate_edits"] == attempts
        await m.stop()
    asyncio.run(body())


def test_retry_limit_is_keyed_persisted_across_round_reset_and_reset_by_apply(tmp_path):
    edit = [{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]
    delegate = Delegate(_reply(edit), _reply(edit), Completion(content="no idea"), _reply(edit))

    async def body():
        m = _manager(tmp_path, delegate)
        s, root = await _session(m)
        sid = s["id"]
        for i, paths in enumerate([["other.py"], ["./other.py"]], 1):  # the same edit however the path is spelled
            await _run(m, sid, call("delegate_edit", i, paths=paths, instruction="bump"))
        # A round reset clears the conversation but not the run's counter.
        m.runner._round_reset(m.db.get_session(sid), m.db.get_session(sid)["context"], 0, 3.0, 0)
        assert m.db.get_session(sid)["run"]["delegate_edits"] == {"paths:other.py": 2}
        third = call("delegate_edit", 3, paths=["other.py"], instruction="bump")
        await _run(m, sid, third)
        assert "not JSON" in _payload(m, sid, third)["error"]  # a failed delegation still counts
        fourth = call("delegate_edit", 4, paths=["other.py"], instruction="bump")
        await _run(m, sid, fourth)
        assert "already delegated 3 times" in _payload(m, sid, fourth)["error"]
        assert len(delegate.requests) == 3  # the refused call never reached the model

        # A different key has its own allowance; applying it clears only that key.
        tasked = call("delegate_edit", 5, paths=["other.py"], instruction="bump", task_id="bump-value")
        await _run(m, sid, tasked)
        await _run(m, sid, call("apply_delegated_edit", 6, patch_id=_payload(m, sid, tasked)["patch_id"]))
        assert m.db.get_session(sid)["run"]["delegate_edits"] == {"paths:other.py": 3}
        assert (root / "other.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        await m.stop()
    asyncio.run(body())


def test_delegate_tokens_recorded_on_success_and_on_a_failed_call_with_usage(tmp_path):
    delegate = Delegate(Completion(content="garbage", prompt_tokens=30, completion_tokens=4),
                        LLMError("server down"),
                        _reply([{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}], p=20, c=6))

    async def body():
        m = _manager(tmp_path, delegate)
        s, _ = await _session(m)
        sid = s["id"]
        before = m.db.get_session(sid)
        for i in range(1, 4):
            await _run(m, sid, call("delegate_edit", i, paths=["other.py"], instruction="bump"))
        after = m.db.get_session(sid)
        expected = {"prompt_tokens": 50, "completion_tokens": 10, "calls": 2}  # the LLMError returned no usage
        assert after["totals"]["delegate_tokens"] == expected and after["run"]["delegate_tokens"] == expected
        assert after["totals"]["prompt_tokens"] == before["totals"].get("prompt_tokens", 0) + 50
        assert after["totals"]["completion_tokens"] == before["totals"].get("completion_tokens", 0) + 10
        assert after["totals"].get("turns") == before["totals"].get("turns")
        assert after["run"]["completion_tokens"] == before["run"]["completion_tokens"]  # the budget is the worker's
        await m.stop()
    asyncio.run(body())


def test_input_cap_and_binary_are_tool_errors_that_do_not_count(tmp_path):
    delegate = Delegate()

    async def body():
        m = _manager(tmp_path, delegate, context_tokens=1000)  # input cap 0.5 * 1000 * 3.0 = 1500 characters
        s, root = await _session(m, {"a.py": "a" * 1000, "b.py": "b" * 1000, "c.bin": "x\x00y"})
        sid = s["id"]
        big = call("delegate_edit", 1, paths=["a.py", "b.py"], instruction="x")
        binary = call("delegate_edit", 2, paths=["c.bin"], instruction="x")
        await _run(m, sid, big, binary)
        assert "total 2000 characters; a delegation may read at most 1500" in _payload(m, sid, big)["error"]
        assert "NUL byte" in _payload(m, sid, binary)["error"]
        assert delegate.requests == [] and not m.db.get_session(sid)["run"].get("delegate_edits")
        await m.stop()
    asyncio.run(body())


def test_tools_offered_only_to_local_tower_agent_sessions_with_a_workspace(tmp_path):
    class _Ws:
        def schemas(self):
            return []

    async def body():
        m = _manager(tmp_path, Delegate())
        s, _ = await _session(m)
        sid = s["id"]
        names = lambda sess: {t["function"]["name"] for t in m.runner.tool_schemas(sess, _Ws())}
        assert set(delegate_edit.TOOL_NAMES) <= names(s)
        for change in ({"kind": "chat"}, {"target": "mac"}, {"backend": "claude"}, {"workspace": ""},
                       {"workspace_removed": True}):
            assert not set(delegate_edit.TOOL_NAMES) & names({**s, **change}), change
            assert not m.runner.delegate_edit_available({**s, **change})

        # A session that isn't offered the tool can't call it either.
        m.db.update_session(sid, kind="chat")
        c = call("delegate_edit", 1, paths=["app.py"], instruction="x")
        await _run(m, sid, c)
        assert "unknown tool" in _result(m, sid, c)
        await m.stop()
    asyncio.run(body())


def test_apply_is_decided_and_previewed_as_an_edit_of_each_file(tmp_path):
    delegate = Delegate(_reply([{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]))

    async def body():
        m = _manager(tmp_path, delegate)
        s, _ = await _session(m)
        sid = s["id"]
        propose = call("delegate_edit", 1, paths=["other.py"], instruction="bump")
        await _run(m, sid, propose)
        patch_id = _payload(m, sid, propose)["patch_id"]
        s = m.db.get_session(sid)
        assert m.runner._decide(s, "apply_delegated_edit", {"patch_id": patch_id}).action == "allow"
        m.runner.policy = lambda _s: Policy([{"tool": "edit_file", "path": "other.py", "action": "ask",
                                              "reason": "guarded file"}])
        decision = m.runner._decide(s, "apply_delegated_edit", {"patch_id": patch_id})
        assert decision.action == "ask" and decision.reason == "guarded file"
        detail, _, error = await m.runner._ask_details(s, "apply_delegated_edit", {"patch_id": patch_id},
                                                      m.runner.workspace(s), "guarded file")
        assert error is None and "-VALUE = 1" in detail and "+VALUE = 2" in detail
        _, _, error = await m.runner._ask_details(s, "apply_delegated_edit", {"patch_id": "p-nope"},
                                                  m.runner.workspace(s), "")
        assert "unknown patch_id" in error

        m.runner.policy = lambda _s: Policy([{"tool": "edit_file", "action": "deny", "reason": "read-only"}])
        apply = call("apply_delegated_edit", 2, patch_id=patch_id)
        await _run(m, sid, apply)
        assert "blocked by policy (read-only)" in _result(m, sid, apply)
        await m.stop()
    asyncio.run(body())


def test_apply_rolls_back_earlier_files_and_raises_tool_error_when_a_write_fails(tmp_path, monkeypatch):
    import builtins
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("Y = 1\n", encoding="utf-8")
    files = FileOps(tmp_path, 8000)
    texts = {"a.py": "X = 1\n", "b.py": "Y = 1\n"}
    edits = [{"path": "a.py", "old_text": "X = 1", "new_text": "X = 2"},
             {"path": "b.py", "old_text": "Y = 1", "new_text": "Y = 2"}]
    _, proposal = delegate_edit.new_proposal(texts, edits, "k", "s")
    real_open = builtins.open

    def flaky(path, mode="r", *a, **kw):
        if "w" in mode and Path(path).name == "b.py" and not flaky.failed:
            flaky.failed = True
            raise PermissionError("denied")
        return real_open(path, mode, *a, **kw)
    flaky.failed = False
    monkeypatch.setattr(builtins, "open", flaky)
    with pytest.raises(ToolError, match="rolled back"):
        delegate_edit.apply(files, proposal)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "X = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "Y = 1\n"


def test_surrogate_text_is_rejected_before_any_file_is_touched(tmp_path):
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
    texts = {"a.py": "X = 1\n"}
    reply = json.dumps({"edits": [{"path": "a.py", "old_text": "X = 1", "new_text": "X = \ud800"}]})
    with pytest.raises(ToolError, match="UTF-8"):
        delegate_edit.parse_reply(reply, "stop", texts)
    # a proposal that slipped past parsing still must not truncate the file
    files = FileOps(tmp_path, 8000)
    _, proposal = delegate_edit.new_proposal(
        texts, [{"path": "a.py", "old_text": "X = 1", "new_text": "X = \ud800"}], "k", "s")
    with pytest.raises(ToolError):
        delegate_edit.apply(files, proposal)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "X = 1\n"


def test_non_oserror_write_failure_rolls_back_and_raises_tool_error(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("Y = 1\n", encoding="utf-8")
    files = FileOps(tmp_path, 8000)
    texts = {"a.py": "X = 1\n", "b.py": "Y = 1\n"}
    edits = [{"path": "a.py", "old_text": "X = 1", "new_text": "X = 2"},
             {"path": "b.py", "old_text": "Y = 1", "new_text": "Y = 2"}]
    _, proposal = delegate_edit.new_proposal(texts, edits, "k", "s")
    monkeypatch.setattr(delegate_edit, "apply_to_texts", lambda t, e: {"a.py": "X = 2\n", "b.py": "Y = \ud800"})
    with pytest.raises(ToolError, match="rolled back"):
        delegate_edit.apply(files, proposal)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "X = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "Y = 1\n"


def test_content_based_edit_rules_apply_to_apply_delegated_edit(tmp_path):
    delegate = Delegate(_reply([{"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2  # secret"}]))

    async def body():
        m = _manager(tmp_path, delegate)
        s, _ = await _session(m)
        sid = s["id"]
        propose = call("delegate_edit", 1, paths=["other.py"], instruction="bump")
        await _run(m, sid, propose)
        patch_id = _payload(m, sid, propose)["patch_id"]
        s = m.db.get_session(sid)
        direct = {"path": "other.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2  # secret"}
        for action, args in (("deny", {"new_text": "secret"}), ("ask", {"old_text": "VALUE = 1"})):
            rules = [{"tool": "edit_file", "args": args, "action": action, "reason": "content rule"}]
            m.runner.policy = lambda _s, rules=rules: Policy(rules)
            assert m.runner._decide(s, "edit_file", direct).action == action
            decision = m.runner._decide(s, "apply_delegated_edit", {"patch_id": patch_id})
            assert decision.action == action and decision.reason == "content rule"
        m.runner.policy = lambda _s: Policy([{"tool": "edit_file", "args": {"new_text": "nomatch"},
                                              "action": "deny"}])
        assert m.runner._decide(s, "apply_delegated_edit", {"patch_id": patch_id}).action == "allow"
        await m.stop()
    asyncio.run(body())
