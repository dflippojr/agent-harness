"""Deliberately minimal agent loop over an OpenAI-compatible chat endpoint.

This is the baseline harness for the bake-off, not the Phase 1 daemon. It keeps
the moving parts few so differences between runs come from the model.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .sandbox import Sandbox, truncate_middle

SYSTEM_PROMPT = """You are a software agent working inside a project workspace.
You act only through the provided tools. File paths are relative to the workspace root.
Shell commands run in a Linux container at /workspace with Python 3.12, pytest, and git installed, and no network access.

Work methodically: look around before editing, prefer `search` over reading large files in full, and verify changes by running the relevant command or tests.
When the task is complete, call `finish` with your final answer (or a short summary of what you changed), or reply with the answer as a plain message. Don't give a final answer until the work is actually done and verified."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories under a path in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
                    "max_depth": {"type": "integer", "description": "How many levels to descend. Default 2."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file with line numbers. Returns at most 400 lines per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based first line. Default 1."},
                    "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search file contents for a regular expression. Returns up to 100 'path:line: text' matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "File or directory to search. Default '.'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Parent directories are created.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact snippet in a file. old_text must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in the workspace container and return its exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Seconds. Default 60, max 300."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the task and report the final answer or a summary of the work.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
    },
]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


class ToolError(Exception):
    pass


class Workspace:
    """File tools run host-side against the bind-mounted workspace; shell runs in the sandbox."""

    def __init__(self, root: Path, sandbox: Sandbox, read_lines: int = 400, read_chars: int = 20000):
        self.root = root.resolve()
        self.sandbox = sandbox
        self.read_lines = read_lines
        self.read_chars = read_chars

    def resolve(self, path: str | None) -> Path:
        path = (path or ".").strip()
        if path.startswith("/workspace"):
            path = path[len("/workspace"):]
        candidate = (self.root / path.lstrip("/\\")).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise ToolError(f"path escapes the workspace: {path}")
        return candidate

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def list_files(self, path: str = ".", max_depth: int = 2) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries: list[str] = []

        def walk(d: Path, depth: int) -> None:
            for child in sorted(d.iterdir()):
                if child.name in SKIP_DIRS:
                    continue
                entries.append(self.rel(child) + ("/" if child.is_dir() else ""))
                if len(entries) >= 300:
                    return
                if child.is_dir() and depth < max_depth:
                    walk(child, depth + 1)

        walk(base, 1)
        suffix = "\n... (listing truncated at 300 entries)" if len(entries) >= 300 else ""
        return "\n".join(entries) + suffix if entries else "(empty directory)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or len(lines), start + self.read_lines - 1)
        body = "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, end + 1))
        body = truncate_middle(body, self.read_chars)
        if end < len(lines):
            body += f"\n... ({len(lines)} lines total; continue with start_line={end + 1})"
        return body or "(empty file)"

    def search(self, pattern: str, path: str = ".") -> str:
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        base = self.resolve(path)
        files = [base] if base.is_file() else sorted(
            f for f in base.rglob("*") if f.is_file() and not SKIP_DIRS.intersection(f.parts)
        )
        matches: list[str] = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{self.rel(f)}:{n}: {line[:300]}")
                    if len(matches) >= 100:
                        return "\n".join(matches) + "\n... (stopped at 100 matches)"
        return "\n".join(matches) or "no matches"

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"wrote {len(content)} characters to {self.rel(p)}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ToolError(f"old_text must appear exactly once, found {count} occurrences")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old_text, new_text))
        return f"edited {self.rel(p)}"

    def run_shell(self, command: str, timeout: int = 60) -> str:
        code, output = self.sandbox.exec(command, timeout=max(1, min(int(timeout), 300)))
        return f"exit code {code}\n{truncate_middle(output)}"


@dataclass
class AgentResult:
    answer: str = ""
    finished: bool = False
    stop_reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    invalid_tool_calls: int = 0
    tool_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_seconds: float = 0.0
    prompt_tps: list[float] = field(default_factory=list)
    gen_tps: list[float] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        base_url: str,
        model: str,
        workspace: Workspace,
        sampling: dict | None = None,
        max_turns: int = 30,
        wall_limit: float = 900,
        max_tokens: int = 8192,
    ):
        self.base_url = base_url
        self.model = model
        self.ws = workspace
        self.sampling = sampling or {}
        self.max_turns = max_turns
        self.wall_limit = wall_limit
        self.max_tokens = max_tokens
        self.tools = copy.deepcopy(TOOLS)
        for tool in self.tools:
            if tool["function"]["name"] == "read_file":
                tool["function"]["description"] = (f"Read a text file with line numbers. Returns at most "
                                                   f"{workspace.read_lines} lines per call.")

    def _chat(self, messages: list[dict], timeout: float) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": self.tools,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            **self.sampling,
        }
        resp = httpx.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _dispatch(self, name: str, args: dict) -> str:
        handlers = {
            "list_files": self.ws.list_files,
            "read_file": self.ws.read_file,
            "search": self.ws.search,
            "write_file": self.ws.write_file,
            "edit_file": self.ws.edit_file,
            "run_shell": self.ws.run_shell,
        }
        return handlers[name](**args)

    def run(self, task_prompt: str) -> AgentResult:
        result = AgentResult()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task_prompt}]
        result.messages = messages
        started = time.monotonic()
        idle_turns = 0

        while True:
            elapsed = time.monotonic() - started
            if result.turns >= self.max_turns:
                result.stop_reason = "max_turns"
                break
            if elapsed >= self.wall_limit:
                result.stop_reason = "wall_limit"
                break
            result.turns += 1
            data = None
            for attempt in range(3):
                try:
                    data = self._chat(messages, timeout=self.wall_limit - elapsed + 30)
                    break
                except RuntimeError as e:
                    # llama-server returns 500 when it can't parse the model's tool-call syntax.
                    # That's a model formatting failure: count it and resample.
                    if "HTTP 500" in str(e) and attempt < 2:
                        result.invalid_tool_calls += 1
                        continue
                    result.stop_reason = f"request_error: {e}"
                    break
                except httpx.HTTPError as e:
                    result.stop_reason = f"request_error: {e}"
                    break
            if data is None:
                break

            usage = data.get("usage") or {}
            result.prompt_tokens += usage.get("prompt_tokens", 0)
            result.completion_tokens += usage.get("completion_tokens", 0)
            timings = data.get("timings") or {}
            if timings.get("prompt_n", 0) >= 64:
                result.prompt_tps.append(timings["prompt_per_second"])
            if timings.get("predicted_n", 0) >= 16:
                result.gen_tps.append(timings["predicted_per_second"])

            msg = data["choices"][0]["message"]
            assistant = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("reasoning_content"):
                assistant["reasoning_content"] = msg["reasoning_content"]
            calls = msg.get("tool_calls") or []
            if calls:
                assistant["tool_calls"] = calls
            messages.append(assistant)

            if not calls:
                # A plain reply with no tool calls is the final answer, as in mainstream harnesses.
                if assistant["content"].strip():
                    result.answer = assistant["content"]
                    result.finished = True
                    result.stop_reason = "final_message"
                    break
                idle_turns += 1
                if idle_turns >= 3:
                    result.stop_reason = "empty_replies"
                    break
                messages.append({
                    "role": "user",
                    "content": "Continue the task using the tools. When you are done, call `finish` with your answer.",
                })
                continue
            idle_turns = 0

            for call in calls:
                result.tool_calls += 1
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as e:
                    result.invalid_tool_calls += 1
                    output = f"Error: tool arguments were not a valid JSON object ({e})."
                else:
                    if name == "finish":
                        result.answer = str(args.get("answer", ""))
                        result.finished = True
                        output = "Task finished."
                    elif name not in {t["function"]["name"] for t in TOOLS}:
                        result.invalid_tool_calls += 1
                        output = f"Error: unknown tool '{name}'."
                    else:
                        try:
                            output = self._dispatch(name, args)
                        except TypeError as e:
                            result.invalid_tool_calls += 1
                            output = f"Error: bad arguments for {name}: {e}"
                        except (ToolError, OSError) as e:
                            result.tool_errors += 1
                            output = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
                if result.finished:
                    break
            if result.finished:
                result.stop_reason = "finished"
                break

        result.wall_seconds = time.monotonic() - started
        return result
