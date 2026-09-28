"""Issue #158: tool_output config, per-project overrides, and verify-check parsing."""

from __future__ import annotations

import shutil

from harness import config
from harness.config import (Project, ToolOutputConfig, VerifyCheck, _tool_output, _verify_checks,
                            clamp_tool_limit, resolve_tool_output)


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


def test_tool_output_loaded_from_yaml(tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(config.ROOT / "config", cfg_dir)
    loaded = config.load(cfg_dir, tmp_path / "data")
    assert loaded.tool_output == ToolOutputConfig()
    text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    (cfg_dir / "harness.yaml").write_text(
        text.replace("read_file_lines: 400", "read_file_lines: nope")
            .replace("search_matches: 100", "search_matches: 12"), encoding="utf-8")
    again = config.load(cfg_dir, tmp_path / "data")
    assert again.tool_output.read_file_lines == 400
    assert again.tool_output.search_matches == 12
