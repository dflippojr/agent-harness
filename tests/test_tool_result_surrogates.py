"""#777: a native tool result carrying a lone UTF-16 surrogate is recorded and shown to the model, not a failed run.

Tool text is sanitised at the tool-result boundary: each lone surrogate becomes U+FFFD, so the result hashes, the
artifact store and session database stay valid UTF-8, and the model request encodes.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from harness.llm import Completion
from harness.manager import Manager
from harness.tools import Workspace
from test_daemon import Script, call, events, make_cfg, wait_status

LONE = "\ud800"
BIG = "row " * 6000 + LONE + " tail \udfff"  # 24,000+ characters: over the run_shell cap and the mask threshold
CLEAN = BIG.replace(LONE, "\ufffd").replace("\udfff", "\ufffd")


class _FakeWeb:
    tool_names = {"web_fetch"}

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "web_fetch", "description": "fetch",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}]

    async def call(self, name, args):
        return BIG


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _install(m: Manager, tool: str, monkeypatch) -> None:
    """Make `tool` return BIG through its real dispatch route."""
    if tool == "web_fetch":
        m.runner.web = _FakeWeb()
    elif tool == "run_shell":
        orig = m.runner.workspace

        def wrapped(s):
            ws = orig(s)

            async def fake_exec(command, timeout=120, network=False):
                return 0, BIG
            ws.sandbox.exec = fake_exec
            return ws
        m.runner.workspace = wrapped
    else:
        real = Workspace.call

        async def fake_call(self, name, args):
            return BIG if name == tool else await real(self, name, args)
        monkeypatch.setattr(Workspace, "call", fake_call)


@pytest.mark.parametrize("tool,args", [("web_fetch", {"url": "big"}), ("run_shell", {"command": "cat big"}),
                                       ("read_file", {"path": "big.txt"})])
def test_lone_surrogate_result_is_recorded_hashed_and_shown(tmp_path, monkeypatch, tool, args):
    script = Script([Completion(tool_calls=[call(tool, 1, **args)]), Completion(content="done")])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        _install(m, tool, monkeypatch)
        await m.start()
        try:
            sid = m.create("read it")["id"]
            final = await wait_status(m, sid, "done", "failed")
            assert final["status"] == "done", final["stop_reason"]
            (result,) = events(m, sid, "tool_result")
            assert result["ok"] is True
            assert LONE not in result["output"] and "\ufffd" in result["output"]
            stored = m.db.full_artifact(sid, result["artifact_id"])
            assert stored is not None and result["artifact_id"] == _digest(stored)
            assert CLEAN in stored  # the whole result, every lone surrogate replaced
            seen = script.requests[1][-1]["content"]  # the model saw the result
            assert "\ufffd tail \ufffd" in seen and LONE not in seen
        finally:
            await m.stop()
    asyncio.run(body())


@pytest.mark.parametrize("text,expected", [
    ("plain ascii", "plain ascii"),
    ("caf\u00e9 \U0001f600", "caf\u00e9 \U0001f600"),
    ("a\ud800b", "a\ufffdb"),
    ("\udc00start", "\ufffdstart"),
    ("end\ud83d", "end\ufffd"),
    ("\udfff\ud800", "\ufffd\ufffd"),
    ("pair \ud83d\ude00 joined", "pair \U0001f600 joined"),
])
def test_utf8_text_replaces_only_lone_surrogates(text, expected):
    from harness.tools import utf8_text
    out = utf8_text(text)
    assert out == expected
    out.encode("utf-8")  # always encodable
