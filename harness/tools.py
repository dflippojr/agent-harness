"""Tools v1. File tools run host-side against the bind-mounted workspace; commands run in the sandbox.
MacBook sessions use the same schemas through harness.remote.RemoteWorkspace."""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path

from .fileops import (FILE_TOOLS, SKIP_DIRS, FileOps, ToolError, dir_size, normalize_path, resolve_path,  # noqa: F401
                      truncate_middle)
from .sandbox import Sandbox, run_cmd

SHELL_DESCRIPTIONS = {
    "tower": ("Run a shell command in the Linux sandbox at /workspace and return exit code and output. "
              "There is no network unless network is true, which needs the user's approval "
              "(use it for package installs or fetching)."),
    "macbook": ("Run a bash command natively on the user's MacBook (macOS, Apple silicon) in the workspace directory "
                "and return exit code and output. It runs in a sandbox: writes are limited to the workspace, temp and "
                "build caches, and there is no network unless network is true, which needs the user's approval."),
}


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


def tool_schemas(read_lines: int, target: str = "tower") -> list[dict]:
    clone = ("Clone a git repository into the workspace. url is an https URL, or local:<name> for a repository "
             "hosted on this machine." if target == "tower" else "Clone a git repository (https URL) into the workspace.")
    return [
        _fn("list_files", "List files and directories under a workspace path.", {
            "path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
            "max_depth": {"type": "integer", "description": "Levels to descend. Default 2."},
        }),
        _fn("read_file", f"Read a text file with line numbers, at most {read_lines} lines per call. "
                         "If the result says there are more lines, keep reading before you conclude anything.", {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line. Default 1."},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
        }, ["path"]),
        _fn("search", "Search file contents for a regular expression. Returns up to 200 'path:line: text' matches.", {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search. Default '.'"},
        }, ["pattern"]),
        _fn("write_file", "Create or overwrite a file. Parent directories are created.", {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, ["path", "content"]),
        _fn("edit_file", "Replace an exact snippet in a file. old_text must appear exactly once.", {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        }, ["path", "old_text", "new_text"]),
        _fn("run_shell", SHELL_DESCRIPTIONS[target], {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds. Default 120, max 1800."},
            "network": {"type": "boolean", "description": "Allow internet access for this command. Default false."},
        }, ["command"]),
        _fn("git_clone", clone, {
            "url": {"type": "string"},
            "dest": {"type": "string", "description": "Target directory in the workspace. Default: the repo name."},
            "branch": {"type": "string"},
        }, ["url"]),
        _fn("update_notes", "Replace your saved notes for this task. During long tasks older conversation may be "
                            "condensed, but saved notes are always kept verbatim: record intermediate results and "
                            "progress here.", {
            "notes": {"type": "string"},
        }, ["notes"]),
        _fn("finish", "End the task and report the final answer or a summary of the work.", {
            "answer": {"type": "string"},
        }, ["answer"]),
    ]


def validate_args(schema: dict, args: dict) -> dict:
    """Strict argument check. Coerces numeric/boolean strings, which local models often send."""
    params = schema["function"]["parameters"]
    props = params["properties"]
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}; allowed: {', '.join(props)}")
    missing = [k for k in params.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    out = {}
    for key, value in args.items():
        kind = props[key].get("type")
        if kind == "integer":
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"argument {key} must be an integer")
        elif kind == "boolean":
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise ToolError(f"argument {key} must be true or false")
        elif kind == "string" and not isinstance(value, str):
            raise ToolError(f"argument {key} must be a string")
        out[key] = value
    return out


def shell_result(code: int, output: str, network: bool, output_chars: int) -> str:
    text = f"exit code {code}\n{truncate_middle(output, output_chars)}"
    if code != 0 and not network and re.search(r"Temporary failure in name resolution|Could not resolve host|"
                                               r"Network is unreachable|nodename nor servname", output):
        text += "\n[hint: the sandbox has no network; rerun with network: true to request access]"
    return text


class Workspace:
    """Tools for a tower session: file tools run host-side on the bind-mounted workspace, commands in Docker."""

    target = "tower"

    def __init__(self, root: Path, sandbox: Sandbox, repos_dir: Path, context_tokens: int, homelab=None):
        self.files = FileOps(root, context_tokens)
        self.root = self.files.root
        self.sandbox = sandbox
        self.repos_dir = repos_dir
        self.homelab = homelab  # homelab.Homelab for projects with homelab: true
        self.read_lines = self.files.read_lines
        self.output_chars = max(8000, int(context_tokens * 0.08 * 3.5))

    def resolve(self, path: str | None) -> Path:
        return self.files.resolve(path)

    def rel(self, p: Path) -> str:
        return self.files.rel(p)

    async def preview(self, name: str, args: dict) -> str:
        return await asyncio.to_thread(self.files.preview_diff, name, args)

    async def size_bytes(self) -> int:
        return await asyncio.to_thread(dir_size, self.root)

    # sandbox tools (async)
    async def run_shell(self, command: str, timeout: int = 120, network: bool = False) -> str:
        code, output = await self.sandbox.exec(command, timeout=max(1, min(int(timeout), 1800)), network=network)
        return shell_result(code, output, network, self.output_chars)

    async def git_clone(self, url: str, dest: str | None = None, branch: str | None = None) -> str:
        url = url.strip()
        if url.startswith("local:"):
            name = url[len("local:"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
                raise ToolError(f"bad local repository name: {name}")
            source = next((c for c in (self.repos_dir / name, self.repos_dir / f"{name}.git") if c.is_dir()), None)
            if source is None:
                available = sorted(p.name for p in self.repos_dir.iterdir()) if self.repos_dir.is_dir() else []
                raise ToolError(f"no local repository {name!r}; available: {', '.join(available) or 'none'}")
            default_dest = name.removesuffix(".git")
        elif re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/[^\s'\"`$\\]+", url):
            source = None
            default_dest = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        else:
            raise ToolError("url must be https://... or local:<name>")
        target = self.resolve(dest or default_dest)
        if target == self.root or target.exists() and any(target.iterdir()):
            raise ToolError(f"destination already exists and is not empty: {self.rel(target)}")
        rel = self.rel(target)
        branch_args = ["--branch", branch] if branch else []
        if source is not None:
            code, out, err = await run_cmd(
                ["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks", *branch_args, "--",
                 str(source), str(target)], timeout=600)
            output = out + err
        else:
            cmd = " ".join(shlex.quote(a) for a in ["git", "clone", *branch_args, "--", url, rel])
            code, output = await self.sandbox.exec(cmd, timeout=600, network=True)
        if code != 0:
            raise ToolError(f"git clone failed (exit {code}): {truncate_middle(output.strip(), 2000)}")
        return f"cloned {url} into {rel}"

    def schemas(self) -> list[dict]:
        extra = []
        if self.homelab is not None:
            from .homelab import schemas
            extra = schemas(self.homelab.cfg)
        return tool_schemas(self.read_lines) + extra

    async def call(self, name: str, args: dict) -> str:
        if self.homelab is not None and name in self.homelab.tool_names:
            return await self.homelab.call(name, args)
        if name in ("run_shell", "git_clone"):
            return await getattr(self, name)(**args)
        return await asyncio.to_thread(getattr(self.files, name), **args)
