"""Each hosted backend's translator replays a recorded transcript into the neutral events (#991).

A translator is pure: these tests feed it a transcript and compare what it returns, with no Manager, no database and
no Docker. A change in a vendor's stream format shows up here as a fixture diff, not as a runtime surprise.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.backends.events import Assistant, Delta, Error, Result, Session, ToolCall, ToolResult
from harness.backends.print_mode import PrintModeTranslator
from harness.cli_backends import OpencodeJsonl

FIXTURES = Path(__file__).parent / "fixtures"


def replay(translator, records) -> list:
    return [event for record in records for event in translator.translate(record)]


def jsonl(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


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
