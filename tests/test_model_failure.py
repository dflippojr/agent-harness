"""#915: a model call that fails after its retries ends the run as a model failure, not as an internal crash."""

from __future__ import annotations

import asyncio
import logging

import pytest

from harness.llm import LLMError
from harness.manager import Manager

from test_daemon import Script, events, make_cfg, wait_status


def _run_until(tmp_path, step, status: str, setup=None) -> tuple[Manager, dict]:
    """Run one local session whose model answers `step` on every attempt; return the manager and the final row."""
    script = Script([step])
    result: dict = {}

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        if setup is not None:
            setup(m)
        await m.start()
        s = m.create("go")
        result["session"] = await wait_status(m, s["id"], status)
        result["error_events"] = events(m, s["id"], "error")
        result["retries"] = events(m, s["id"], "llm_retry")
        result["summary"] = m.summary(result["session"])
        await m.stop()
        result["manager"] = m
    asyncio.run(body())
    return result["manager"], result


def _runner_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("harness")]


@pytest.mark.parametrize("error,retryable,attempts", [
    (LLMError("HTTP 500: failed to parse tool call", retryable=True), True, 4),
    (LLMError("HTTP 400: the request was rejected", retryable=False), False, 1),
])
def test_model_error_after_retries_is_a_model_failure(tmp_path, caplog, error, retryable, attempts):
    caplog.set_level(logging.DEBUG)
    calls = {"n": 0}

    def failing(msgs):
        calls["n"] += 1
        return error

    _, r = _run_until(tmp_path, failing, "failed")
    s = r["session"]
    failure = s["run"]["failure"]
    assert failure["code"] == "model_error"
    assert failure["retryable"] is retryable
    assert failure["provider"] == "local"
    assert failure["message"] == str(error)
    assert s["stop_reason"].startswith("model_error:")
    assert str(error) in s["stop_reason"]
    assert r["error_events"] == [failure]
    assert r["summary"]["failure"] == failure
    assert calls["n"] == attempts
    assert len(r["retries"]) == attempts - 1

    records = _runner_records(caplog)
    assert not [rec for rec in records if rec.levelno >= logging.ERROR]
    assert not [rec for rec in records if rec.exc_info]
    warnings = [rec for rec in records if rec.levelno == logging.WARNING and s["id"] in rec.getMessage()]
    assert len(warnings) == 1
    assert "crashed" not in warnings[0].getMessage()


def test_long_model_error_warning_is_capped(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    _, r = _run_until(tmp_path, lambda msgs: LLMError("HTTP 400: " + "x" * 600, retryable=False), "failed")
    warning = next(rec for rec in _runner_records(caplog) if r["session"]["id"] in rec.getMessage())
    assert len(warning.getMessage()) < 300
    assert r["session"]["run"]["failure"]["message"] == "HTTP 400: " + "x" * 600


def test_other_exception_in_a_local_session_is_still_an_internal_error(tmp_path, caplog):
    caplog.set_level(logging.WARNING)

    def boom(msgs):
        return RuntimeError("injected crash")

    _, r = _run_until(tmp_path, boom, "failed")
    s = r["session"]
    assert s["run"]["failure"]["code"] == "internal_error"
    assert s["run"]["failure"]["retryable"] is True
    assert s["stop_reason"].startswith("internal_error: RuntimeError: injected crash")
    crashed = [rec for rec in _runner_records(caplog) if "crashed" in rec.getMessage()]
    assert len(crashed) == 1
    assert crashed[0].levelno == logging.ERROR
    assert crashed[0].exc_info


def test_cancel_racing_a_model_error_ends_cancelled(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    box: dict = {}

    def keep(m: Manager) -> None:
        box["m"] = m

    def cancel_then_fail(msgs):
        runner = box["m"].runner
        runner.user_cancelled.update(runner.tasks.keys() if hasattr(runner, "tasks") else box["m"].tasks.keys())
        return LLMError("HTTP 400: rejected while a cancel was pending", retryable=False)

    _, r = _run_until(tmp_path, cancel_then_fail, "cancelled", setup=keep)
    s = r["session"]
    assert s["status"] == "cancelled"
    assert s["stop_reason"] == "cancelled"
    assert s["run"].get("failure") is None
    assert r["error_events"] == []
    assert not [rec for rec in _runner_records(caplog) if rec.levelno >= logging.ERROR]
