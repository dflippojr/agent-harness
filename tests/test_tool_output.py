"""Issue #158: tool_output config, per-project overrides, and verify-check parsing. #670: result_chars."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

import pytest

from harness import config
from harness.config import (Project, ToolOutputConfig, VerifyCheck, _tool_output, _verify_checks,
                            clamp_tool_limit, resolve_tool_output)
from harness.llm import Completion
from harness.manager import Manager
from harness.tools import bound_shell_text, truncate_middle
from harness.verify import ARTIFACT_FOOTER
from test_daemon import Script, call, events, make_cfg, wait_status


def test_tool_output_defaults_and_invalid_fallback():
    defaults = _tool_output(None)
    assert defaults == ToolOutputConfig()
    assert _tool_output({}) == ToolOutputConfig()
    assert _tool_output("nope") == ToolOutputConfig()
    assert _tool_output({"read_file_lines": "nope", "search_matches": 0, "run_shell_chars": True,
                         "verify_summary_chars": 10**9}).read_file_lines == 400
    parsed = _tool_output({
        "read_file_lines": 50, "read_file_lines_max": 80, "search_matches": 10, "search_matches_max": 20,
        "run_shell_chars": 1000, "run_shell_chars_max": 2000, "verify_summary_chars": 500,
    })
    assert parsed.read_file_lines == 50 and parsed.read_file_lines_max == 80
    assert parsed.search_matches == 10 and parsed.run_shell_chars == 1000
    assert parsed.verify_summary_chars == 500
    # Default above its max is clamped down, not stacked or rejected.
    assert _tool_output({"read_file_lines": 900, "read_file_lines_max": 100}).read_file_lines == 100


def test_clamp_tool_limit_honors_configured_maximum():
    assert clamp_tool_limit(None, 400, 2000) == 400
    assert clamp_tool_limit(800, 400, 2000) == 800
    assert clamp_tool_limit(9999, 400, 2000) == 2000
    assert clamp_tool_limit(0, 400, 2000) == 1
    assert clamp_tool_limit("50", 400, 2000) == 50


def test_verify_checks_skip_invalid_entries():
    checks = _verify_checks([
        {"name": "tests", "command": "pytest --tb=short -ra", "timeout": 90, "parser": "pytest"},
        {"name": "lint", "command": "ruff check ."},
        {"name": "bad"}, {"command": "only"}, "nope",
        {"name": "slow", "command": "sleep 1", "timeout": 9999, "parser": "mystery"},
    ])
    assert [c.name for c in checks] == ["tests", "lint", "slow"]
    assert checks[0].parser == "pytest" and checks[0].timeout == 90
    assert checks[1].parser == "" and checks[1].timeout == 120
    assert checks[2].timeout == 1800 and checks[2].parser == ""
    assert _verify_checks(None) == [] and _verify_checks("x") == []


def test_project_from_spec_keeps_tool_output_and_verify():
    project = config._project_from_spec("demo", {
        "description": "d",
        "tool_output": {"read_file_lines": 20, "search_matches": 5},
        "verify": [{"name": "tests", "command": "pytest --tb=short -ra", "timeout": 30}],
    })
    assert project.tool_output["read_file_lines"] == 20
    assert project.verify == [VerifyCheck(name="tests", command="pytest --tb=short -ra", timeout=30, parser="")]
    empty = config._project_from_spec("web", {"description": "from the web app"})
    assert empty.tool_output == {} and empty.verify == []


def test_resolve_tool_output_overlays_project_on_global(tmp_path):
    cfg = config.Config(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", repos_dir=tmp_path / "repos",
        default_model="fake", models={}, sandbox=config.SandboxConfig(),
        projects={}, tool_output=ToolOutputConfig(read_file_lines=400, search_matches=100),
    )
    project = Project(name="p", tool_output={"read_file_lines": 10, "bogus": 1})
    resolved = resolve_tool_output(cfg, project)
    assert resolved.read_file_lines == 10 and resolved.search_matches == 100
    assert resolve_tool_output(cfg, Project(name="plain")) == cfg.tool_output


def test_invalid_project_override_falls_back_to_global_not_package_default(tmp_path):
    # #254: the owner raised search_matches globally; a project typo must not drop it back to 100.
    cfg = config.Config(
        host="127.0.0.1", port=0, data_dir=tmp_path / "data", repos_dir=tmp_path / "repos",
        default_model="fake", models={}, sandbox=config.SandboxConfig(),
        projects={}, tool_output=ToolOutputConfig(search_matches=300, run_shell_chars=50_000),
    )
    for bad in ("lots", 0, 10**9, True, None):
        resolved = resolve_tool_output(cfg, Project(name="p", tool_output={"search_matches": bad}))
        assert resolved.search_matches == 300, bad
    resolved = resolve_tool_output(cfg, Project(name="p", tool_output={"run_shell_chars": "x", "read_file_lines": 7}))
    assert resolved.run_shell_chars == 50_000 and resolved.read_file_lines == 7


@pytest.mark.parametrize("key, value, read, expected", [
    ("mask_min_chars", 5000, lambda c: c.mask_min_chars, 5000),
    ("mask_min_chars", 0, lambda c: c.mask_min_chars, None),
    ("mask_min_chars", "many", lambda c: c.mask_min_chars, None),
    ("read_file_lines", "nope", lambda c: c.tool_output.read_file_lines, None),
    ("search_matches", 12, lambda c: c.tool_output.search_matches, 12),
    ("result_chars", 3000, lambda c: c.tool_output.result_chars, 3000),
    ("result_chars", "lots", lambda c: c.tool_output.result_chars, None),
    ("reset_at", "no", lambda c: c.reset_at, None),
    ("state_max_chars", 12, lambda c: c.state_max_chars, None),
    (None, None, lambda c: c.tool_output, config.ToolOutputConfig()),
], ids=["mask-set", "mask-zero", "mask-text", "read-lines-text", "search-set", "result-chars-set", "result-chars-text",
        "reset-at-text", "state-chars-low", "tool-output-defaults"])
def test_harness_yaml_keys_reach_the_loaded_config(tmp_path, key, value, read, expected):
    """A valid value reaches the loaded config; an invalid one falls back to the shipped value (`expected` None)."""
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(config.ROOT / "config", cfg_dir)
    shipped = read(config.load(cfg_dir, tmp_path / "data"))
    if key is not None:
        path = cfg_dir / "harness.yaml"
        text, found = re.subn(rf"^(\s*{key}:).*$", rf"\g<1> {value}", path.read_text(encoding="utf-8"), flags=re.M)
        assert found == 1
        path.write_text(text, encoding="utf-8")
    assert read(config.load(cfg_dir, tmp_path / "data")) == (shipped if expected is None else expected)


def test_read_file_default_page_is_400_and_max_lines_clamps(tmp_path):
    from harness.fileops import FileOps
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("\n".join(f"L{i}" for i in range(1, 901)), encoding="utf-8")
    files = FileOps(ws, 8000)
    text = files.read_file("big.txt")
    assert text.startswith("1\tL1")
    assert "400\tL400" in text
    assert "401\tL401" not in text
    assert "... (900 lines total; continue with start_line=401)" in text
    raised = files.read_file("big.txt", max_lines=2000)
    assert "900\tL900" in raised
    assert "continue with" not in raised
    clamped = files.read_file("big.txt", max_lines=9999)
    assert clamped.count("\n") <= 2000  # read_lines_max default
    page2 = files.read_file("big.txt", start_line=401)
    assert page2.startswith("401\tL401")
    assert "continue with start_line=801" in page2


def test_search_offset_reports_at_least_n_without_scanning_total(tmp_path):
    from harness.fileops import FileOps
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("\n".join(f"hit {i}" for i in range(250)), encoding="utf-8")
    files = FileOps(ws, 8000, search_matches=100, search_matches_max=500)
    first = files.search("hit")
    assert first.startswith("a.txt:1: hit 0")
    assert "... (showing matches 1-100 of at least 100; continue with offset=100)" in first
    assert "hit 100" not in first.split("...")[0]
    page2 = files.search("hit", offset=100)
    assert "showing matches 101-200 of at least 200" in page2
    assert "continue with offset=200" in page2
    last = files.search("hit", offset=200)
    assert "hit 249" in last
    assert "showing matches" not in last  # finished without hitting the cap
    raised = files.search("hit", max_matches=500)
    assert "hit 249" in raised
    assert files.search("hit", offset=999) == "no matches"


def test_result_chars_defaults_and_clamps_to_its_max():
    """#670: result_chars bounds every other native tool result; above result_chars_max it is clamped down."""
    assert (ToolOutputConfig().result_chars, ToolOutputConfig().result_chars_max) == (20000, 100000)
    assert _tool_output({"result_chars": 500_000, "result_chars_max": 1000}).result_chars == 1000
    assert _tool_output({"result_chars": 0, "result_chars_max": "x"}) == ToolOutputConfig()
    parsed = _tool_output({"result_chars": 3000, "result_chars_max": 9000})
    assert (parsed.result_chars, parsed.result_chars_max) == (3000, 9000)
    cfg = config.Config(
        host="127.0.0.1", port=0, data_dir=Path("data"), repos_dir=Path("repos"), default_model="fake", models={},
        sandbox=config.SandboxConfig(), projects={}, tool_output=ToolOutputConfig(result_chars=50_000),
    )
    assert resolve_tool_output(cfg, Project(name="p", tool_output={"result_chars": 700})).result_chars == 700
    assert resolve_tool_output(cfg, Project(name="p", tool_output={"result_chars": "x"})).result_chars == 50_000


PAGE = "".join(f"line {i:05d} of a long fetched page\n" for i in range(1000))  # 34,000 characters


class _FakeWeb:
    tool_names = {"web_fetch"}

    def schemas(self):
        return [{"type": "function", "function": {
            "name": "web_fetch", "description": "fetch",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}}]

    async def call(self, name, args):
        return PAGE if args["url"] == "big" else PAGE[:5000]


def test_a_large_web_fetch_is_stored_whole_and_returned_as_head_tail_with_a_footer(tmp_path):
    digest = hashlib.sha256(PAGE.encode("utf-8")).hexdigest()
    script = Script([Completion(tool_calls=[call("web_fetch", 0, url="big")]),
                     Completion(tool_calls=[call("read_artifact", 1, artifact_id=digest, start=0, end=20000)]),
                     Completion(tool_calls=[call("web_fetch", 2, url="small")]),
                     Completion(content="done")])

    async def body():
        m = Manager(make_cfg(tmp_path), chat=script)
        m.runner.web = _FakeWeb()
        await m.start()
        try:
            sid = m.create("read the page")["id"]
            s = await wait_status(m, sid, "done", "failed")
            assert s["status"] == "done", s["stop_reason"]
            fetched, recovered, small = events(m, sid, "tool_result")
            seen = script.requests[1][-1]["content"]
            footer = ARTIFACT_FOOTER.format(total=len(PAGE), artifact_id=digest)
            assert seen == bound_shell_text(PAGE, 20000, digest) and seen.endswith(footer)
            assert seen.startswith(PAGE[:5000]) and seen.removesuffix("\n" + footer).endswith(PAGE[-5000:])
            assert fetched["artifact_id"] == digest and fetched["output_chars"] == len(seen)
            assert m.db.full_artifact(sid, digest) == PAGE
            # The footer's id recovers the full text; the recovered range is never stored again.
            assert recovered["ok"] and recovered["output"] == PAGE[:20000] and recovered["artifact_id"] is None
            assert script.requests[2][-1]["content"] == PAGE[:20000]
            # Under the limit the result is untouched.
            assert script.requests[3][-1]["content"] == PAGE[:5000] == small["output"]
        finally:
            await m.stop()
    asyncio.run(body())


def test_a_project_lowers_result_chars_and_read_artifact_output_is_not_bounded_again(tmp_path):
    digest = hashlib.sha256(PAGE.encode("utf-8")).hexdigest()
    script = Script([Completion(tool_calls=[call("web_fetch", 0, url="big")]),
                     Completion(tool_calls=[call("read_artifact", 1, artifact_id=digest, start=0, end=9000)]),
                     Completion(content="done")])
    cfg = make_cfg(tmp_path)
    cfg.projects["scratch"] = Project(name="scratch", tool_output={"result_chars": 3000})

    async def body():
        m = Manager(cfg, chat=script)
        m.runner.web = _FakeWeb()
        await m.start()
        try:
            sid = m.create("read the page")["id"]
            await wait_status(m, sid, "done")
            fetched, recovered = events(m, sid, "tool_result")
            assert script.requests[1][-1]["content"] == bound_shell_text(PAGE, 3000, digest)
            assert fetched["artifact_id"] == digest
            # read_artifact is the recovery path: 9000 characters come back whole, above the project's 3000.
            assert script.requests[2][-1]["content"] == PAGE[:9000]
            assert recovered["artifact_id"] is None and "output truncated" not in recovered["output"]
            assert m.db.full_artifact(sid, hashlib.sha256(PAGE[:9000].encode("utf-8")).hexdigest()) is None
        finally:
            await m.stop()
    asyncio.run(body())


def test_a_session_without_read_artifact_gets_plain_middle_truncation(tmp_path):
    cfg = make_cfg(tmp_path, rules=[{"tool": "read_artifact", "action": "deny", "reason": "no recovery"}])
    script = Script([Completion(tool_calls=[call("web_fetch", 0, url="big")]), Completion(content="done")])

    async def body():
        m = Manager(cfg, chat=script)
        m.runner.web = _FakeWeb()
        await m.start()
        try:
            sid = m.create("read the page", project="guarded")["id"]
            s = await wait_status(m, sid, "done")
            assert not m.runner.artifact_tool_available(s)
            seen = script.requests[1][-1]["content"]
            assert seen == truncate_middle(PAGE, 20000)
            assert "read_artifact" not in seen
            assert events(m, sid, "tool_result")[0]["artifact_id"] is None
            assert m.db.full_artifact(sid, hashlib.sha256(PAGE.encode("utf-8")).hexdigest()) is None
        finally:
            await m.stop()
    asyncio.run(body())


def test_truncate_middle_at_the_smallest_limits_drops_the_middle():
    """#670 review: limit 1 made half 0, and text[-0:] kept the whole text."""
    text = "x" * 35_000
    for limit in (1, 2, 3):
        cut = truncate_middle(text, limit)
        assert len(cut) < 60 and "characters truncated" in cut, limit


def test_result_chars_of_one_still_bounds_a_large_result(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.projects["scratch"] = Project(name="scratch", tool_output={"result_chars": 1})
    script = Script([Completion(tool_calls=[call("web_fetch", 0, url="big")]), Completion(content="done")])

    async def body():
        m = Manager(cfg, chat=script)
        m.runner.web = _FakeWeb()
        await m.start()
        try:
            sid = m.create("read the page")["id"]
            await wait_status(m, sid, "done")
            digest = hashlib.sha256(PAGE.encode("utf-8")).hexdigest()
            seen = script.requests[1][-1]["content"]
            assert seen.endswith(ARTIFACT_FOOTER.format(total=len(PAGE), artifact_id=digest))
            assert len(seen) < 300 and m.db.full_artifact(sid, digest) == PAGE
        finally:
            await m.stop()
    asyncio.run(body())
