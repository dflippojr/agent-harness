"""OpenCode (`opencode run --format json`, 1.18.30): the first config-declared print-mode CLI's parser (#168)."""

from __future__ import annotations

import json
import re

from .events import obj


class OpencodeJsonl:
    """`opencode run --format json` (OpenCode 1.18.30) records as print-mode events.

    Each line is one object with a top-level `sessionID`. `text` parts carry the assistant's words, grouped by
    `part.messageID` (the last message's text is the answer); `tool_use` arrives once a tool has finished, with
    `part.state.status` `completed` or `error`; `step_finish` closes one model step; `error` ends the turn in error.
    Lines that are not JSON objects (a warning OpenCode prints, a partial line) are skipped.
    """

    # The managed config dir outranks every other config source but macOS MDM (config/managed.ts in 1.18.30), so
    # neither the workspace's opencode.json nor its .opencode/ can turn the denials back on.
    config_target = "/etc/opencode/opencode.json"
    env = ("OPENCODE_DISABLE_PROJECT_CONFIG=1", "OPENCODE_DISABLE_AUTOUPDATE=1")

    def __init__(self) -> None:
        self._texts: dict[str, list[str]] = {}

    @staticmethod
    def config(model, base_url: str) -> str:
        """The opencode.json for a session, from the harness `models:` entry (Q6). The `question` denial is required:
        a headless question would hang the turn."""
        return json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "provider": {"llama": {
                "npm": "@ai-sdk/openai-compatible", "name": "llama-server (agent-harness)",
                "options": {"baseURL": base_url, "apiKey": "local"},
                "models": {model.name: {"name": model.name, "tool_call": True, "reasoning": True,
                                        "limit": {"context": model.context_tokens, "output": model.max_tokens}}},
            }},
            "permission": {"*": "allow", "webfetch": "deny", "websearch": "deny", "question": "deny"},
        }, indent=2)

    @staticmethod
    def erase_args(session_id: str) -> list[str]:
        """Delete one session from OpenCode's own store (a lookup by id, whatever the project)."""
        return ["opencode", "session", "delete", session_id]

    @staticmethod
    def erase_missing(output: str, session_id: str) -> bool:
        """Whether a failed delete only means the session is already gone: OpenCode 1.18.30 prints exactly
        `Error: Session not found: <id>` (in colour). Any other failure, such as no opencode in the image, is real."""
        return f"Session not found: {session_id}" in re.sub(r"\x1b\[[0-9;]*m", "", output)

    @property
    def answer(self) -> str:
        """The text of the last assistant message that had any."""
        return "\n".join(list(self._texts.values())[-1]).strip() if self._texts else ""

    def parse(self, line: str) -> list[dict]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        out: list[dict] = []
        if isinstance(event.get("sessionID"), str) and event["sessionID"]:
            out.append({"type": "session", "id": event["sessionID"]})
        part = obj(event, "part")
        type_ = event.get("type")
        text = part.get("text")
        if type_ == "text" and isinstance(text, str) and text:
            self._texts.setdefault(str(part.get("messageID") or ""), []).append(text)
            out.append({"type": "text", "text": text})
        elif type_ == "tool_use":
            state = obj(part, "state")
            failed = state.get("status") == "error"
            output = state.get("error") if failed else state.get("output")
            out.append({"type": "tool", "id": str(part.get("callID") or part.get("id") or ""),
                        "name": str(part.get("tool") or "tool"), "status": str(state.get("status") or ""),
                        "error": failed, "input": obj(state, "input"),
                        "output": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)})
        elif type_ == "step_finish":
            out.append({"type": "step"})
        elif type_ == "error":
            error = obj(event, "error")
            message = str(obj(error, "data").get("message") or "OpenCode reported an error")
            out.append({"type": "error", "message": f"{error.get('name') or 'Error'}: {message}"[:1000]})
        return out
