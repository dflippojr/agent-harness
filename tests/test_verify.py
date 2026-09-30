"""Issue #158: verify parsers, summary bound, raw-log artifact, and runner wiring."""

from __future__ import annotations

import asyncio
import hashlib
import json

from harness.config import Project, VerifyCheck
from harness.fileops import ToolError, cap_command_output, CappedStream, CAPTURE_CAPPED_NOTE, OUTPUT_CAP
from harness.llm import Completion
from harness.manager import Manager
from harness.tools import bound_shell_text, shell_result, tool_schemas
from harness.verify import (ARTIFACT_FOOTER, bound_rendered, infer_parser, normalize_message, parse_generic,
                            parse_pytest, render_verify, run_verify)

from test_daemon import Script, call, events, make_cfg, wait_status


def _pytest_log(groups: list[tuple[str, int, str, str, int]]) -> str:
    """Build `--tb=short -ra` output: groups of (file, line, test, message, count)."""
    tb, summary, n = [], [], 0
    for path, line, test, message, count in groups:
        for i in range(count):
            name = test if count == 1 else f"{test}_{i}"
            tb.append(f"{path}:{line}: in {name}\n    assert False\nE   {message}")
            summary.append(f"FAILED {path}::{name} - {message}")
            n += 1
    return (
        "============================= test session starts ==============================\n"
        "=================================== FAILURES ===================================\n"
        + "\n".join(tb)
        + "\n=========================== short test summary info ============================\n"
        + "\n".join(summary)
        + f"\n========================= {n} failed in 1.23s =========================\n"
    )


def test_pytest_parser_collapses_40_failures_to_3_root_causes():
    log = _pytest_log([
        ("tests/test_a.py", 10, "test_a", "assert 1 == 2", 20),
        ("tests/test_b.py", 4, "test_b", "ValueError: boom", 15),
        ("tests/test_c.py", 8, "test_c", "assert None", 5),
    ])
    parsed = parse_pytest(log)
    assert len(parsed) == 40
    from harness.verify import _dedup
    groups = _dedup("tests", parsed)
    assert len(groups) == 3
    assert [g["count"] for g in groups] == [20, 15, 5]
    rendered = render_verify({"ok": False, "checks": [{"name": "tests", "code": 1, "timed_out": False}],
                              "failures": groups})
    assert " (×20)" in rendered and " (×15)" in rendered and " (×5)" in rendered
    assert "tests/test_a.py:10" in rendered
    assert json.dumps(groups) not in rendered


def test_generic_fallback_keeps_error_lines_else_log_tail():
    mixed = "ok\nFAILED something\ninfo\nerror: nope\n"
    items = parse_generic(mixed)
    assert [i["message"] for i in items] == ["FAILED something", "error: nope"]
    tail_only = "hello world\n" * 100
    items = parse_generic(tail_only)
    assert len(items) == 1 and items[0]["kind"] == "log"
    assert items[0]["message"] == tail_only[-2000:]


def test_generic_20mb_log_summary_stays_at_4000_chars():
    log = "ok\n" * 100 + ("x" * (20 * 1024 * 1024))
    items = parse_generic(log)
    rendered = render_verify({"ok": False, "checks": [{"name": "lint", "code": 1, "timed_out": False}],
                              "failures": [{**items[0], "check": "lint", "count": 1}]})
    capped = bound_rendered(rendered, 4000, log)
    assert len(capped) < 5000
    assert len(log) >= 20 * 1024 * 1024
    assert log[:1000] not in capped and ("x" * 5000) not in capped
    # Many distinct error lines force the 4,000-character bound and the artifact footer.
    noisy = "\n".join(f"error {i}: {i}" for i in range(400))
    noisy_items = [{**it, "check": "lint", "count": 1} for it in parse_generic(noisy)]
    noisy_rendered = render_verify({"ok": False, "checks": [{"name": "lint", "code": 1, "timed_out": False}],
                                    "failures": noisy_items})
    assert len(noisy_rendered) > 4000
    noisy_capped = bound_rendered(noisy_rendered, 4000, noisy)
    body, _, footer = noisy_capped.rpartition("\n")
    assert len(body) <= 4000
    digest = hashlib.sha256(noisy.encode("utf-8")).hexdigest()
    assert ARTIFACT_FOOTER.format(total=len(noisy), artifact_id=digest) == footer
    assert "the next page" not in noisy_capped.lower()


def test_bound_rendered_omits_read_artifact_when_unavailable():
    noisy = "\n".join(f"error {i}: {i}" for i in range(400))
    noisy_items = [{**it, "check": "lint", "count": 1} for it in parse_generic(noisy)]
    noisy_rendered = render_verify({"ok": False, "checks": [{"name": "lint", "code": 1, "timed_out": False}],
                                    "failures": noisy_items})
    assert len(noisy_rendered) > 4000
    capped = bound_rendered(noisy_rendered, 4000, noisy, artifact_available=False)
    assert "read_artifact" not in capped
    assert len(capped) <= 4000
    offered = bound_rendered(noisy_rendered, 4000, noisy)
    assert "recover with read_artifact" in offered
    assert hashlib.sha256(noisy.encode("utf-8")).hexdigest() in offered


def test_normalize_message_strips_timings_pids_and_hex():
    a = normalize_message("boom after 1.23s pid=4321 at 0x7fff1234")
    b = normalize_message("boom after 9.00s pid=1 at 0xABCDEF")
    assert a == b
    assert "<time>" in a and "pid=<pid>" in a and "<hex>" in a


def test_parsers_stay_linear_on_long_adversarial_lines():
    import time

    def _log(n: int) -> str:
        spaces, digits = " " * n, "9" * n
        return (f"FAILED{spaces}tests/test_a.py::test_a - boom\n"
                f"ERROR{spaces}tests/test_b.py::test_b\n"
                f"FAILED {digits}::test_c\n")

    def _normalize_shapes(n: int) -> None:
        spaces = " " * n
        normalize_message(f"crash pid{spaces}4321 after 1.23s at 0xabc")
        normalize_message("x pid" + spaces)  # ReDoS shape: pid + spaces, no digits
        normalize_message("pid" + spaces + "=" + spaces)

    def _timed(fn) -> float:
        t0 = time.perf_counter()
        fn()
        return max(time.perf_counter() - t0, 1e-3)

    small, large = 2_000, 200_000
    parsed = parse_pytest(_log(large))
    norm = normalize_message(f"crash pid{' ' * large}4321 after 1.23s at 0xabc")
    for name, fn in (
        ("parse_pytest", lambda n: parse_pytest(_log(n))),
        ("parse_generic", lambda n: parse_generic(_log(n))),
        ("normalize_message", _normalize_shapes),
    ):
        small_time = _timed(lambda fn=fn: fn(small))
        large_time = _timed(lambda fn=fn: fn(large))
        budget = small_time * (large / small) * 20
        assert large_time < budget, (
            f"{name} looks super-linear: {small_time:.4f}s at {small} chars vs "
            f"{large_time:.4f}s at {large} chars (budget {budget:.4f}s)"
        )
    digits = "9" * large
    assert [p["nodeid"] for p in parsed] == [
        "tests/test_a.py::test_a", "tests/test_b.py::test_b", f"{digits}::test_c"]
    assert parsed[0]["message"] == "boom" and parsed[1]["kind"] == "error"
    assert "pid=<pid>" in norm


def test_infer_parser_from_command_and_explicit_field():
    assert infer_parser(VerifyCheck("t", "pytest --tb=short -ra", parser="")) == "pytest"
    assert infer_parser(VerifyCheck("t", "python -m pytest -q", parser="")) == "pytest"
    assert infer_parser(VerifyCheck("t", "ruff check .", parser="")) == "generic"
    assert infer_parser(VerifyCheck("t", "pytest", parser="generic")) == "generic"


def test_timeout_is_detected_by_exit_124_not_substring():
    async def exec_cmd(command, timeout):
        return 124, "command finished the word timeout in its output"
    check = VerifyCheck("slow", "sleep 999", timeout=2)
    result = asyncio.run(run_verify([check], exec_cmd, 4000))
    assert "TIMED OUT" in result.text
    assert result.extra["verify"]["checks"][0]["timed_out"] is True
    assert "timeout in its output" in result.artifact_content


def test_passing_generic_check_with_error_like_text_has_no_failures():
    async def exec_cmd(command, timeout):
        return 0, "Found 0 errors.\nAll checks passed!\n"
    result = asyncio.run(run_verify([VerifyCheck("lint", "ruff check .")], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert result.text == "verify: all checks passed (1 checks)"
    assert "LOG" not in result.text and "[lint] ERROR" not in result.text
    assert "Found 0 errors" not in result.text
    assert "All checks passed!" not in result.text


def test_passing_pytest_run_has_no_failures():
    log = (
        "============================= test session starts ==============================\n"
        "============================== 3 passed in 0.12s ===============================\n"
    )
    async def exec_cmd(command, timeout):
        return 0, log
    result = asyncio.run(run_verify(
        [VerifyCheck("tests", "pytest --tb=short -ra", parser="pytest")], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert result.text == "verify: all checks passed (1 checks)"
    assert "passed in" not in result.text


def test_failing_generic_check_keeps_error_lines():
    async def exec_cmd(command, timeout):
        return 1, "ok\nerror: nope\nFAILED something\n"
    result = asyncio.run(run_verify([VerifyCheck("lint", "ruff check .")], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is False
    assert [f["message"] for f in payload["failures"]] == ["error: nope", "FAILED something"]
    assert "[lint] ERROR" in result.text
    assert "all checks passed" not in result.text.lower()


def test_timeout_with_empty_output_is_still_a_failure():
    async def exec_cmd(command, timeout):
        return 124, ""
    result = asyncio.run(run_verify([VerifyCheck("slow", "sleep 9", timeout=2)], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is False
    assert payload["checks"][0]["timed_out"] is True
    assert payload["failures"]
    assert "TIMED OUT" in result.text
    # #254: one line for the check and a single count in the header, not "1 error, 1 timed out".
    assert [ln for ln in result.text.splitlines() if ln.startswith("[slow]")] == [
        "[slow] TIMED OUT after 2s (exit 124)"]
    assert result.text.splitlines()[0] == "verify: 1 timed out (1 checks)"


def test_hung_check_log_tail_is_not_a_second_failure():
    async def exec_cmd(command, timeout):
        return 124, "starting server\nwaiting for connections\n"
    result = asyncio.run(run_verify([VerifyCheck("serve", "npm start", timeout=5)], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is False
    assert [f["kind"] for f in payload["failures"]] == ["timeout"]
    assert [ln for ln in result.text.splitlines() if ln.startswith("[serve]")] == [
        "[serve] TIMED OUT after 5s (exit 124)"]
    assert result.text.splitlines()[0] == "verify: 1 timed out (1 checks)"
    assert "waiting for connections" in result.artifact_content


def test_timed_out_check_keeps_distinct_parsed_failures():
    async def exec_cmd(command, timeout):
        return 124, "error: build step failed\n"
    result = asyncio.run(run_verify([VerifyCheck("build", "make", timeout=5)], exec_cmd, 4000))
    lines = [ln for ln in result.text.splitlines() if ln.startswith("[build]")]
    assert lines == ["[build] TIMED OUT after 5s (exit 124)", "[build] ERROR error: build step failed"]
    assert result.text.splitlines()[0] == "verify: 1 error, 1 timed out (1 checks)"


def test_mixed_passing_and_failing_checks_only_parse_failures():
    async def exec_cmd(command, timeout):
        if "ruff" in command:
            return 0, "All checks passed!\nFound 0 errors.\n"
        if "pytest" in command:
            return 1, _pytest_log([("tests/test_a.py", 3, "test_a", "assert False", 1)])
        return 0, "ok"
    result = asyncio.run(run_verify([
        VerifyCheck("lint", "ruff check ."),
        VerifyCheck("tests", "pytest --tb=short -ra", parser="pytest"),
    ], exec_cmd, 4000))
    payload = result.extra["verify"]
    assert payload["ok"] is False
    assert all(f["check"] == "tests" for f in payload["failures"])
    assert any("assert False" in (f.get("message") or "") or "failed" in (f.get("kind") or "")
               for f in payload["failures"])
    assert "All checks passed" not in result.text
    assert "Found 0 errors" not in result.text
    assert "[lint]" not in result.text
    assert "test_a" in result.text


def test_no_configured_checks_errors():
    async def exec_cmd(command, timeout):
        raise AssertionError("must not run")
    try:
        asyncio.run(run_verify([], exec_cmd, 4000))
    except ToolError as e:
        assert str(e) == "no configured checks"
    else:
        raise AssertionError("expected ToolError")


def test_shell_result_does_not_truncate_and_footer_never_says_next_page():
    output = "y" * 50000
    text = shell_result(0, output, False)
    assert text == f"exit code 0\n{output}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    bounded = bound_shell_text(text, 20000, digest)
    assert "characters truncated" in bounded
    assert ARTIFACT_FOOTER.format(total=len(text), artifact_id=digest) in bounded
    assert "the next page" not in bounded.lower()
    assert bounded.count(digest) == 1


def test_cap_command_output_matches_one_million():
    huge = "z" * 1_500_000
    capped = cap_command_output(huge)
    assert len(capped) < 1_100_000
    assert capped.startswith("z" * 100) and capped.endswith("z" * 100)
    assert "... [output cut] ..." in capped
    stream = CappedStream()
    for i in range(0, len(huge), 70_000):
        stream.feed(huge[i:i + 70_000])
    assert stream.capped and stream.text() == capped
    assert CAPTURE_CAPPED_NOTE in stream.get()
    under = CappedStream()
    under.feed("ok")
    assert not under.capped and under.get() == "ok"


def test_capture_cap_is_reported_in_artifact_truncation_and_footer():
    stream = CappedStream()
    stream.feed("A" * (OUTPUT_CAP + 50_000))
    captured = stream.get()
    assert CAPTURE_CAPPED_NOTE in captured
    text = shell_result(0, captured, False)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    bounded = bound_shell_text(text, 20000, digest)
    assert CAPTURE_CAPPED_NOTE in text  # stored artifact content
    assert CAPTURE_CAPPED_NOTE in bounded  # visible in the middle-truncated tail
    assert f"characters total (capture capped); recover with read_artifact(artifact_id={digest}" in bounded
    noisy = "\n".join(f"error {i}: {i}" for i in range(400)) + "\n" + CAPTURE_CAPPED_NOTE
    noisy_items = [{**it, "check": "lint", "count": 1} for it in parse_generic(noisy)]
    noisy_rendered = render_verify({"ok": False, "checks": [{"name": "lint", "code": 1, "timed_out": False}],
                                    "failures": noisy_items})
    footer = bound_rendered(noisy_rendered, 4000, noisy).rpartition("\n")[2]
    assert "characters total (capture capped);" in footer
    assert hashlib.sha256(noisy.encode("utf-8")).hexdigest() in footer


def test_verify_is_in_managed_schemas_and_search_mentions_at_least():
    names = [t["function"]["name"] for t in tool_schemas(400)]
    assert "verify" in names
    search = next(t for t in tool_schemas(400) if t["function"]["name"] == "search")
    assert "at least" in search["function"]["description"]
    assert "offset" in search["function"]["parameters"]["properties"]


def test_verify_and_run_shell_store_full_output_not_the_view(tmp_path):
    full = "shell-body-" + ("N" * 25000)
    cfg = make_cfg(tmp_path)
    cfg.projects["scratch"] = Project(
        name="scratch",
        verify=[VerifyCheck("tests", "pytest --tb=short -ra", timeout=30, parser="pytest")],
    )
    script = Script([
        Completion(tool_calls=[call("run_shell", 0, command="echo big")]),
        Completion(tool_calls=[call("verify", 1)]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        orig = m.runner.workspace

        def wrapped(s):
            ws = orig(s)

            async def fake_exec(command, timeout=120, network=False):
                if "pytest" in command:
                    assert network is False
                    return 1, _pytest_log([("tests/test_a.py", 3, "test_a", "assert False", 2)])
                return 0, full
            ws.sandbox.exec = fake_exec
            return ws

        m.runner.workspace = wrapped
        await m.start()
        s = await wait_status(m, m.create("check")["id"], "done")
        results = events(m, s["id"], "tool_result")
        shell_ev, verify_ev = results[0], results[1]
        shell_digest = hashlib.sha256(f"exit code 0\n{full}".encode("utf-8")).hexdigest()
        assert shell_ev["artifact_id"] == shell_digest
        assert "output truncated" in shell_ev["output"]
        assert m.db.full_artifact(s["id"], shell_digest).startswith("exit code 0\n" + full[:20])
        assert "the next page" not in shell_ev["output"]
        # Compaction masks the used shell result; the receipt must recover the full string, not the view.
        receipts = [m["content"] for m in m.db.get_session(s["id"])["context"]
                    if m.get("role") == "tool" and (m.get("content") or "").startswith("[Observation receipt]")]
        assert any(shell_digest in r for r in receipts)
        verify_id = verify_ev["artifact_id"]
        raw = m.db.full_artifact(s["id"], verify_id)
        assert "FAILED tests/test_a.py" in verify_ev["output"]
        assert "(×2)" in verify_ev["output"]
        assert "verify" in verify_ev and "failures" in verify_ev["verify"]
        assert json.dumps(verify_ev["verify"]) not in verify_ev["output"]
        assert raw and "FAILED tests/test_a.py" in raw and "test session starts" in raw
        assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == verify_id
        assert verify_ev["output"] != raw
        await m.stop()
    asyncio.run(body())


def test_verify_without_project_checks_returns_error(tmp_path):
    script = Script([
        Completion(tool_calls=[call("verify", 0)]),
        Completion(content="ok"),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        orig = m.runner.workspace

        def wrapped(s):
            ws = orig(s)

            async def fake_exec(command, timeout=120, network=False):
                raise AssertionError("verify must not exec when no checks are configured")
            ws.sandbox.exec = fake_exec
            return ws

        m.runner.workspace = wrapped
        await m.start()
        s = await wait_status(m, m.create("v")["id"], "done")
        out = events(m, s["id"], "tool_result")[0]["output"]
        assert "no configured checks" in out
        await m.stop()
    asyncio.run(body())


def test_run_shell_put_artifact_failure_does_not_point_at_unstored_id(tmp_path):
    full = "shell-body-" + ("N" * 25000)
    stored = f"exit code 0\n{full}"
    digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    script = Script([
        Completion(tool_calls=[call("run_shell", 0, command="echo big")]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        orig_put = m.db.put_artifact

        def boom(sid, hash_, content):
            if hash_ == digest:
                raise OSError("disk full")
            return orig_put(sid, hash_, content)

        m.db.put_artifact = boom
        orig = m.runner.workspace

        def wrapped(s):
            ws = orig(s)

            async def fake_exec(command, timeout=120, network=False):
                return 0, full
            ws.sandbox.exec = fake_exec
            return ws

        m.runner.workspace = wrapped
        await m.start()
        s = await wait_status(m, m.create("check")["id"], "done")
        ctx = "\n".join(msg.get("content") or "" for msg in m.db.get_session(s["id"])["context"])
        assert digest not in ctx
        assert "recovery unavailable" in ctx
        ev = events(m, s["id"], "tool_result")[0]
        assert ev["artifact_id"] is None
        assert digest not in (ev.get("output") or "")
        assert m.db.full_artifact(s["id"], digest) is None
        await m.stop()
    asyncio.run(body())


def test_verify_footer_follows_artifact_tool_availability(tmp_path):
    """A recover-with-read_artifact footer is only valid when the session can call that tool."""
    noisy = "\n".join(f"error {i}: boom {i}" for i in range(80))
    check = VerifyCheck("lint", "ruff check .")
    overlay = {"verify_summary_chars": 200}
    cfg = make_cfg(tmp_path, rules=[{"tool": "read_artifact", "action": "deny", "reason": "no recovery"}])
    cfg.projects["scratch"] = Project(name="scratch", verify=[check], tool_output=overlay)
    cfg.projects["guarded"] = Project(
        name="guarded",
        rules=[{"tool": "read_artifact", "action": "deny", "reason": "no recovery"}],
        verify=[check],
        tool_output=overlay,
    )
    script = Script([
        Completion(tool_calls=[call("verify", 0)]),
        Completion(content="done"),
    ])

    async def body():
        m = Manager(cfg, chat=script)
        orig = m.runner.workspace

        def wrapped(s):
            ws = orig(s)

            async def fake_exec(command, timeout=120, network=False):
                return 1, noisy
            ws.sandbox.exec = fake_exec
            return ws

        m.runner.workspace = wrapped
        await m.start()
        offered = await wait_status(m, m.create("with artifact", project="scratch")["id"], "done")
        denied = await wait_status(m, m.create("no artifact", project="guarded")["id"], "done")
        offered_out = events(m, offered["id"], "tool_result")[0]["output"]
        denied_out = events(m, denied["id"], "tool_result")[0]["output"]
        digest = hashlib.sha256(f"=== lint (exit 1) ===\n{noisy}".encode("utf-8")).hexdigest()
        assert "recover with read_artifact" in offered_out
        assert digest in offered_out
        assert m.runner.artifact_tool_available(m.db.get_session(offered["id"]))
        assert not m.runner.artifact_tool_available(m.db.get_session(denied["id"]))
        assert "read_artifact" not in denied_out
        assert len(denied_out) <= 200
        assert events(m, denied["id"], "tool_result")[0]["artifact_id"] is None
        await m.stop()
    asyncio.run(body())
