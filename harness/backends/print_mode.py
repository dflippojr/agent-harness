"""Config-declared print-mode CLIs (`kind: cli`, #168): the translator from a parser's events to neutral ones.

A parser registered in `PRINT_MODE_PARSERS` keeps the #168 contract: `parse(line) -> list[event]` over the CLI's
stdout, with `session`, `text`, `tool`, `step` and `error` events, and an `answer` property. The transport adds an
`exit` record when a turn's process ends. `PrintModeTranslator` maps both onto the neutral events, so a new CLI needs
a parser and nothing in the Runner.
"""

from __future__ import annotations

from .events import Assistant, Delta, Error, Event, Result, Session, ToolCall, ToolResult


class PrintModeTranslator:
    """One session's records: stdout lines through `parser`, and the transport's `{"type": "exit", "code", "stderr"}`
    at the end of each turn. `name` is the backend's, for the error messages. Steps are counted over the session's
    turns; they are its turn count."""

    def __init__(self, parser, name: str):
        self.parser = parser
        self.name = name
        self.steps = 0

    def translate(self, record: object) -> list[Event]:
        if isinstance(record, str):
            return [event for item in self.parser.parse(record) for event in self._event(item)]
        if isinstance(record, dict) and record.get("type") == "exit":
            return self._exit(record)
        return []

    def _event(self, item: dict) -> list[Event]:
        type_ = item.get("type")
        if type_ == "session":
            return [Session(str(item["id"]))]
        if type_ == "text":
            return [Delta("content", item["text"])]
        if type_ == "tool":
            # The CLI reports a tool once it has run (auto-approved in its container): the call and its result.
            call = ToolCall(item["id"], item["name"], item["input"])
            return [Assistant(tool_calls=(call,)), ToolResult(item["id"], item["name"], not item["error"],
                                                              item["output"])]
        if type_ == "step":
            self.steps += 1
        elif type_ == "error":
            return [Error(f"{self.name}: {item['message']}")]
        return []

    def _exit(self, record: dict) -> list[Event]:
        """A turn's process ended: an error, or the turn's result (the next queued turn, or the run's end). Usage is
        not reported."""
        code = record.get("code")
        if code != 0:
            detail = f": {record['stderr']}" if record.get("stderr") else ""
            return [Error(f"{self.name} exited with code {code}{detail}")]
        return [Result(answer=self.parser.answer, turns=self.steps, show_answer=True)]
