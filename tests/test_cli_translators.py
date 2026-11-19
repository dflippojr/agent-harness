"""Each hosted backend's translator replays a recorded transcript into the neutral events (#991).

A translator is pure: these tests feed it a transcript and compare what it returns, with no Manager, no database and
no Docker. A change in a vendor's stream format shows up here as a fixture diff, not as a runtime surprise.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.backends.claude import ClaudeTranslator
from harness.backends.codex import ELICITATION, CodexTranslator
from harness.backends.events import (Assistant, Delta, Error, PermissionRequest, RateLimits, Result, Session, ToolCall,
                                     ToolResult)
from harness.backends.print_mode import PrintModeTranslator
from harness.cli_backends import OpencodeJsonl

FIXTURES = Path(__file__).parent / "fixtures"


def replay(translator, records) -> list:
    return [event for record in records for event in translator.translate(record)]


def jsonl(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in jsonl(path)]


# --- config-declared print mode (#168): OpenCode 1.18.30, recorded from the real CLI ------------------------------

OPENCODE = FIXTURES / "opencode_1.18.30"
OPENCODE_SESSION = "ses_f5f0271e7ffeGkAS0Wj99tPl6U"


def test_print_mode_replays_the_recorded_opencode_turn():
    lines = jsonl(OPENCODE / "opencode_turn.jsonl")
    events = replay(PrintModeTranslator(OpencodeJsonl(), "opencode"),
                    [*lines, "not json at all", {"type": "exit", "code": 0, "stderr": ""}])
    assert {e.id for e in events if isinstance(e, Session)} == {OPENCODE_SESSION}  # every record names it
    glob_input, edit_input = (json.loads(lines[i])["part"]["state"]["input"] for i in (1, 4))
    answer = json.loads(lines[7])["part"]["text"]
    assert [e for e in events if not isinstance(e, Session)] == [
        Assistant(tool_calls=(ToolCall("bG4c73rzW6TxoM3AQVeDJlVpcCu1YA4o", "glob", glob_input),)),
        ToolResult("bG4c73rzW6TxoM3AQVeDJlVpcCu1YA4o", "glob", True, "No files found"),
        Assistant(tool_calls=(ToolCall("S9z9KNPXeemwjsYd1guCsTY0R9HM4fbU", "edit", edit_input),)),
        ToolResult("S9z9KNPXeemwjsYd1guCsTY0R9HM4fbU", "edit", False,
                   json.loads(lines[4])["part"]["state"]["error"]),
        Delta("content", answer),
        Result(answer=answer, turns=3, show_answer=True),  # three model steps; usage is not reported
    ]


def test_print_mode_error_record_and_failed_exit_are_errors():
    translator = PrintModeTranslator(OpencodeJsonl(), "opencode")
    errors = [e for e in replay(translator, jsonl(OPENCODE / "opencode_error.jsonl")) if isinstance(e, Error)]
    assert len(errors) == 1 and errors[0].message.startswith("opencode: ContextOverflowError: ")
    assert translator.translate({"type": "exit", "code": 3, "stderr": "llama-server went away"}) == [
        Error("opencode exited with code 3: llama-server went away")]
    assert translator.translate({"type": "exit", "code": 1, "stderr": ""}) == [Error("opencode exited with code 1")]


# --- Claude Code 2.1.272: stream-json (assembled from the verified protocol; see the fixture's README) --------------

CLAUDE = FIXTURES / "claude_code_2.1.272"


def test_claude_replays_a_turn_with_a_permission_request_a_tool_error_and_rate_limits():
    turn = records(CLAUDE / "turn.jsonl")
    sid, bash, read = turn[0]["session_id"], turn[6]["message"]["content"][0], turn[9]["message"]["content"][0]
    limits, result = turn[11]["rate_limit_info"], turn[-1]
    answer = result["result"]
    assert replay(ClaudeTranslator(), turn) == [
        Session(sid),
        # the thinking delta carries `thinking`, not `text`: nothing streams for it (as before #991)
        Delta("content", "I'll run the test suite first."),
        Assistant("I'll run the test suite first."),
        Assistant(tool_calls=(ToolCall(bash["id"], "Bash", bash["input"]),)),
        PermissionRequest(turn[7]["request_id"], "Bash", bash["input"], bash["id"], "Run the test suite"),
        ToolResult(bash["id"], "Bash", True, turn[8]["message"]["content"][0]["content"]),
        Assistant(tool_calls=(ToolCall(read["id"], "Read", read["input"]),)),
        ToolResult(read["id"], "Read", False, "<tool_use_error>File does not exist.</tool_use_error>"),
        RateLimits(limits, limit_reached=False, resets_at=1795046400.0, billing=True),
        Delta("content", "All 3 tests pass."),
        Assistant(answer),
        Result(answer=answer, usage=result["usage"], turns=4, cost=0.1234, subtype="success"),
    ]


def test_claude_rejected_limit_and_rate_limited_result_reach_the_limit():
    limit = records(CLAUDE / "limit.jsonl")
    events = replay(ClaudeTranslator(), limit)
    assert events[0] == Session(limit[0]["session_id"])
    assert events[1] == RateLimits(limit[1]["rate_limit_info"], limit_reached=True, resets_at=1794819600.0,
                                   billing=True)
    assert events[2].is_error and events[2].limit_reached and events[2].subtype == "success"
    # A failed result that isn't about a limit fails the run instead
    failed = ClaudeTranslator().translate({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                           "result": "tool crashed", "num_turns": 1})
    assert failed == [Result(answer="tool crashed", turns=1, is_error=True, subtype="error_during_execution")]


def test_claude_skips_what_is_not_a_session_event():
    translator = ClaudeTranslator()
    assert translator.translate({"type": "control_request", "request_id": "r1",
                                 "request": {"subtype": "hook_callback"}}) == []
    assert translator.translate({"type": "system", "subtype": "init"}) == []  # no session id yet
    assert translator.translate({"type": "user", "message": {"content": "a plain user message"}}) == []
    assert translator.translate("not a record") == []


# --- Codex 0.154.0: app-server JSON-RPC (assembled from the protocol notes; see the fixture's README) ---------------

CODEX = FIXTURES / "codex_0.154.0"


def test_codex_replays_a_turn_with_each_kind_of_approval_tool_errors_and_rate_limits():
    turn = records(CODEX / "turn.jsonl")
    thread = turn[0]["params"]["thread"]["id"]
    cmd, patch, mcp = (turn[i]["params"]["item"] for i in (4, 7, 10))
    run = {"command": cmd["command"], "cwd": "/workspace"}
    edit = {"file_paths": ["/workspace/src/settings.py"], "patch": patch["changes"][0]["diff"]}
    search = {"query": "pytest 9 changelog"}
    answer = turn[15]["params"]["item"]["text"]
    windows = {"five_hour": {"utilization": 0.34, "resetsAt": 1794819600},
               "seven_day": {"utilization": 0.61, "resetsAt": 1795046400}}
    limits = {**turn[13]["params"]["rateLimits"], "status": "allowed", "unifiedWindows": windows,
              "rateLimitType": "seven_day", "utilization": 0.61, "resetsAt": 1795046400.0}
    assert replay(CodexTranslator(), turn) == [
        Session(thread),
        # the response to the harness's turn/start, and turn/started, are the transport's: no events
        Delta("reasoning", "Running the tests"),
        Assistant(tool_calls=(ToolCall(cmd["id"], "exec_command", run),)),
        PermissionRequest(0, "exec_command", run, cmd["id"], "Run the test suite"),
        ToolResult(cmd["id"], "exec_command", True, "...\n3 passed in 0.42s\n", 1.82),
        Assistant(tool_calls=(ToolCall(patch["id"], "apply_patch", edit),)),
        PermissionRequest(1, "apply_patch", edit, patch["id"], "Raise the retry count"),
        ToolResult(patch["id"], "apply_patch", False, edit["patch"]),  # declined
        Assistant(tool_calls=(ToolCall(mcp["id"], "mcp__harness__web_search", search),)),
        PermissionRequest(2, "mcp__harness__web_search", search, mcp["id"],
                          'Allow the harness MCP server to run tool "web_search"?'),
        ToolResult(mcp["id"], "mcp__harness__web_search", False, "search backend unavailable", 0.24),
        RateLimits(limits, limit_reached=False, resets_at=1795046400.0),  # the longest window decides
        Delta("content", "The 3 tests pass."),
        Assistant(answer),
        Result(answer=answer, turns=1, subtype="success", usage={
            "input_tokens": 9050, "output_tokens": 260, "cached_input_tokens": 8192, "reasoning_output_tokens": 96}),
    ]


def test_codex_reached_limit_and_usage_limit_failure_reach_the_limit():
    limit = records(CODEX / "limit.jsonl")
    events = replay(CodexTranslator(), limit)
    assert events[0] == Session(limit[0]["params"]["thread"]["id"])
    assert isinstance(events[1], RateLimits) and events[1].limit_reached and events[1].info["status"] == "rejected"
    error = json.dumps(limit[3]["params"]["turn"]["error"], ensure_ascii=False)
    assert events[2] == Result(answer=error, turns=1, is_error=True, subtype="failed", limit_reached=True, usage={
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_output_tokens": 0})


def test_codex_without_an_environment_stops_on_a_built_in_tool():
    command = {"method": "item/started", "params": {"item": {"type": "commandExecution", "id": "c1", "command": "ls"}}}
    assert CodexTranslator().translate(command) == [
        Assistant(tool_calls=(ToolCall("c1", "exec_command", {"command": "ls", "cwd": ""}),))]
    assert CodexTranslator("tools_only").translate(command) == [
        Error("Codex used a built-in tool (commandExecution) in an App-tools-only session; stopped")]
    other_server = {"method": "item/completed", "params": {"item": {
        "type": "mcpToolCall", "id": "c2", "server": "github", "tool": "list_issues", "status": "completed"}}}
    assert CodexTranslator("split").translate(other_server) == [
        Error("Codex used a built-in tool (mcpToolCall) in a split-mode session; stopped")]


def test_codex_elicitation_binds_once_and_a_form_is_not_decidable():
    translator = CodexTranslator()
    started = {"method": "item/started", "params": {"item": {
        "type": "mcpToolCall", "id": "c1", "server": "harness", "tool": "web_search", "status": "inProgress",
        "arguments": {"query": "q"}}}}
    ask = {"id": 5, "method": ELICITATION, "params": {"serverName": "harness", "message": "Allow?", "_meta": {
        "codex_approval_kind": "mcp_tool_call", "tool_params": {"query": "q"}}}}
    translator.translate(started)
    assert translator.translate(ask) == [
        PermissionRequest(5, "mcp__harness__web_search", {"query": "q"}, "c1", "Allow?")]
    assert translator.translate({**ask, "id": 6}) == [PermissionRequest(6, "", {}, decidable=False)]  # asked already
    form = {"id": 7, "method": ELICITATION, "params": {"serverName": "harness", "mode": "form", "_meta": {}}}
    assert translator.translate(form) == [PermissionRequest(7, "", {}, decidable=False)]
