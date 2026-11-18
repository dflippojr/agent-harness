"""A run that fails mid tool call answers every call it leaves before it ends (#924).

The cancel path (`_record_cancel`) and the deadline path (`_end_on_deadline`) already close unresolved calls; each
failure branch of `Runner._run` must too, or the next run's context carries a tool call with no result."""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from harness import projects, tools
from harness.cli_backends import CliBackendError
from harness.llm import Completion, LLMError
from harness.manager import Manager
from harness.runner import Runner, unresolved_calls
from harness.runner_contract import RunnerError
from harness.sandbox import SandboxUnavailable
from test_daemon import Script, call, make_cfg, wait_status

STOPPED = "Stopped: the run failed while this ran; its effects are unknown."
NOT_RUN = "Not run: the run failed."


def test_a_failed_run_answers_its_executing_call_before_the_next_run(tmp_path, monkeypatch):
    # The issue's reproduction, as given.
    async def proxy_down(self, command, timeout=120, network=False, max_chars=None):
        raise SandboxUnavailable("egress proxy is not running")
    monkeypatch.setattr(tools.Workspace, "run_shell", proxy_down)
    script = Script([Completion(tool_calls=[call("run_shell", 0, command="pip download requests")]),
                     Completion(content="ok")])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        try:
            s = m.create("fetch a package")
            s = await wait_status(m, s["id"], "failed")
            await m.send(s["id"], "I started the proxy, try again")
            await wait_status(m, s["id"], "done")
            sent = script.requests[-1]
            calls = [c["id"] for msg in sent if msg["role"] == "assistant" for c in msg.get("tool_calls") or []]
            answered = {msg.get("tool_call_id") for msg in sent if msg["role"] == "tool"}
            assert [c for c in calls if c not in answered] == []
        finally:
            await m.stop()
    asyncio.run(body())


# One exception per failure branch of Runner._run, raised from inside a running tool call, with the failure code
# that branch records (so a case that lands in another branch fails rather than passing vacuously).
BRANCHES = {
    "CliBackendError": (lambda: CliBackendError("the CLI exited", code="provider_unavailable"),
                        "provider_unavailable"),
    "SandboxUnavailable": (lambda: SandboxUnavailable("egress proxy is not running"), "backend_unavailable"),
    "projects.GitError": (lambda: projects.GitError("index.lock exists"), "workspace_error"),
    "RunnerError": (lambda: RunnerError("the Mac runner restarted", kind="restarted"), "workspace_error"),
    "llm.LLMError": (lambda: LLMError("upstream closed the stream"), "model_error"),
    "Exception": (lambda: RuntimeError("boom"), "internal_error"),
}

# Handlers of Runner._run that are not failures: each already answers open calls, under its own tests.
NOT_FAILURES = {
    "asyncio.CancelledError": "a cancel (_record_cancel) or a time budget (_end_on_deadline); tests/test_phase8.py "
                              "and tests/test_session_admission.py",
    "ApprovalExpired": "_end_on_deadline(approval_expired); tests/test_session_admission.py",
}


@pytest.mark.parametrize("branch", sorted(BRANCHES))
def test_every_failure_branch_answers_the_calls_it_leaves(tmp_path, monkeypatch, branch):
    make_error, code = BRANCHES[branch]

    async def fail(self, command, timeout=120, network=False, max_chars=None):
        raise make_error()
    monkeypatch.setattr(tools.Workspace, "run_shell", fail)
    script = Script([Completion(tool_calls=[call("run_shell", 0, command="make"),
                                            call("list_files", 1)]),
                     Completion(content="ok")])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        await m.start()
        try:
            sid = m.create("build it")["id"]
            s = await wait_status(m, sid, "failed")
            assert s["run"]["failure"]["code"] == code
            assert unresolved_calls(s["context"]) == []
            results = {msg["tool_call_id"]: msg["content"] for msg in s["context"] if msg["role"] == "tool"}
            assert results == {"c0-run_shell": STOPPED, "c1-list_files": NOT_RUN}
            assert (s["run"].get("executing") or None) is None
            assert m.db.pending_approvals(sid) == []
        finally:
            await m.stop()
    asyncio.run(body())


def test_a_failed_run_cancels_its_pending_approval(tmp_path, monkeypatch):
    async def crash(self, aid):
        raise RuntimeError("approval store went away")
    monkeypatch.setattr(Runner, "_wait_approval", crash)
    cfg = make_cfg(tmp_path, rules=[{"tool": "write_file", "action": "ask", "reason": "test"}])
    script = Script([Completion(tool_calls=[call("write_file", 0, path="a.txt", content="x")]),
                     Completion(content="ok")])

    async def body():
        m = Manager(cfg, chat=script)
        await m.start()
        try:
            sid = m.create("write it", project="guarded")["id"]
            s = await wait_status(m, sid, "failed")
            assert s["run"]["failure"]["code"] == "internal_error"
            assert m.db.pending_approvals(sid) == []
            approval = m.db.approval_for_call(sid, "c0-write_file")
            assert approval is not None and approval["status"] == "cancelled"
            results = {msg["tool_call_id"]: msg["content"] for msg in s["context"] if msg["role"] == "tool"}
            assert results == {"c0-write_file": NOT_RUN}
        finally:
            await m.stop()
    asyncio.run(body())


def _run_handler_types() -> set[str]:
    """The exception types the outer try of Runner._run handles, as written ("projects.GitError")."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Runner._run)))
    outer = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
    names: set[str] = set()
    for handler in outer.handlers:
        kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        names.update(ast.unparse(kind) for kind in kinds)
    return names


def test_every_end_path_of_a_run_is_covered_by_the_unanswered_call_check():
    """Class-level check (#924): a new except branch in Runner._run must be added to BRANCHES (and so be tested to
    answer the calls it leaves) or to NOT_FAILURES with the path and tests that answer them."""
    handled = _run_handler_types()
    assert handled, "could not find Runner._run's handlers"
    unlisted = handled - set(BRANCHES) - set(NOT_FAILURES)
    assert not unlisted, (f"Runner._run handles {sorted(unlisted)} without a case here: a run ending that way must "
                          "answer every tool call it leaves (#924)")
    stale = (set(BRANCHES) | set(NOT_FAILURES)) - handled
    assert not stale, f"no longer handled by Runner._run: {sorted(stale)}"
