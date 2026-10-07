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
from .config import ToolOutputConfig
from .verify import ARTIFACT_FOOTER, ToolOutput, run_verify

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


def tool_schemas(read_lines: int, target: str = "tower", *, search_matches: int = 100,
                 read_file_lines_max: int = 2000, search_matches_max: int = 500,
                 run_shell_chars: int = 20000, run_shell_chars_max: int = 100000,
                 include_verify: bool = True) -> list[dict]:
    clone = ("Clone a git repository into the workspace. url is an https URL, or local:<name> for a repository "
             "hosted on this machine." if target == "tower" else "Clone a git repository (https URL) into the workspace.")
    schemas = [
        _fn("list_files", "List files and directories under a workspace path.", {
            "path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"},
            "max_depth": {"type": "integer", "description": "Levels to descend. Default 2."},
        }),
        _fn("read_file", f"Read a text file with line numbers, at most {read_lines} lines per call. "
                         "If the result says there are more lines, keep reading before you conclude anything.", {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line. Default 1."},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
            "max_lines": {"type": "integer",
                          "description": f"Lines to return this call. Default {read_lines}, max {read_file_lines_max}."},
        }, ["path"]),
        _fn("search", f"Search file contents for a regular expression. Returns up to {search_matches} "
                      "'path:line: text' matches starting at offset (0-based match index). Stopping reports "
                      "at least N matches rather than scanning for an exact total; continue with offset from the footer.", {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search. Default '.'"},
            "offset": {"type": "integer", "description": "0-based match index to start from. Default 0."},
            "max_matches": {"type": "integer",
                            "description": f"Matches to return this call. Default {search_matches}, max {search_matches_max}."},
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
            "max_chars": {"type": "integer",
                          "description": f"Characters of output to return. Default {run_shell_chars}, max {run_shell_chars_max}."},
        }, ["command"]),
        _fn("git_clone", clone, {
            "url": {"type": "string"},
            "dest": {"type": "string", "description": "Target directory in the workspace. Default: the repo name."},
            "branch": {"type": "string"},
        }, ["url"]),
        _fn("update_state", "Replace your saved task state with this object (full replacement). Required: goal. "
                            "Optional: plan, errors (failed command + args + message), next_step, notes. Unknown "
                            "fields are rejected. During long tasks the conversation may be condensed or reset; "
                            "saved state is re-injected. Call reset_round to start a new round from this state. "
                            "Do not send files_modified; the harness derives that at reset.", {
            "goal": {"type": "string"},
            "plan": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "object", "properties": {
                "command": {"type": "string"}, "args": {}, "message": {"type": "string"},
            }}},
            "next_step": {"type": "string"},
            "notes": {"type": "string"},
        }, ["goal"]),
        _fn("reset_round", "Clear older conversation and continue from saved state plus this turn. "
                           "Requires a valid saved state from update_state; without one this call returns an "
                           "error and does not schedule a reset. No arguments.", {}),
        _fn("update_notes", "Deprecated alias: set only the notes field of saved state and leave every other "
                            "field unchanged.", {
            "notes": {"type": "string"},
        }, ["notes"]),
        _fn("finish", "End the task and report the final answer or a summary of the work.", {
            "answer": {"type": "string"},
        }, ["answer"]),
    ]
    if include_verify:
        schemas.insert(-5, _fn(
            "verify",
            "Run every project check configured in YAML (no arguments) and return a bounded failure summary. "
            "Commands are owner-trusted and run serially with no network. Pytest parsing needs --tb=short -ra; "
            "other checkers use a generic error/fail scrape. Truncated output is recovered with read_artifact "
            "on the raw log, not by rerunning.",
            {},
        ))
    return schemas


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
    return {key: _coerce_arg(key, props[key].get("type"), value) for key, value in args.items()}


def _coerce_arg(key: str, kind: str | None, value):
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
    return value


def shell_result(code: int, output: str, network: bool, output_chars: int | None = None) -> str:
    """Format exit code and output. Truncation happens after the full string is stored as an artifact."""
    text = f"exit code {code}\n{output}"
    if code != 0 and not network and re.search(r"Temporary failure in name resolution|Could not resolve host|"
                                               r"Network is unreachable|nodename nor servname", output):
        text += "\n[hint: the sandbox has no network; rerun with network: true to request access]"
    return text


def bound_shell_text(text: str, limit: int, artifact_id: str) -> str:
    if len(text) <= limit:
        return text
    footer = ARTIFACT_FOOTER.format(total=len(text), artifact_id=artifact_id)
    if "capture capped at" in text:
        footer = footer.replace("characters total;", "characters total (capture capped);")
    return truncate_middle(text, limit) + "\n" + footer


class Workspace:
    """Tools for a tower session: file tools run host-side on the bind-mounted workspace, commands in Docker."""

    target = "tower"

    def __init__(self, root: Path, sandbox: Sandbox, repos_dir: Path, context_tokens: int, host_toolkits=(),
                 public_clone_only: bool = False, clone_max_bytes: int | None = None,
                 tool_output: ToolOutputConfig | None = None, verify_checks: list | None = None):
        self.tool_output = tool_output or ToolOutputConfig()
        self.verify_checks = list(verify_checks or [])
        self.files = FileOps(root, context_tokens, read_lines=self.tool_output.read_file_lines,
                             read_lines_max=self.tool_output.read_file_lines_max,
                             search_matches=self.tool_output.search_matches,
                             search_matches_max=self.tool_output.search_matches_max)
        self.root = self.files.root
        self.sandbox = sandbox
        self.repos_dir = repos_dir
        self.host_toolkits = tuple(host_toolkits)
        self.public_clone_only = public_clone_only
        self.clone_max_bytes = clone_max_bytes
        self.read_lines = self.files.read_lines
        self.output_chars = self.tool_output.run_shell_chars

    def resolve(self, path: str | None) -> Path:
        return self.files.resolve(path)

    def rel(self, p: Path) -> str:
        return self.files.rel(p)

    async def preview(self, name: str, args: dict) -> str:
        return await asyncio.to_thread(self.files.preview_diff, name, args)

    async def size_bytes(self) -> int:
        return await asyncio.to_thread(dir_size, self.root)

    # sandbox tools (async)
    async def run_shell(self, command: str, timeout: int = 120, network: bool = False, max_chars=None) -> str:
        code, output = await self.sandbox.exec(command, timeout=max(1, min(int(timeout), 1800)), network=network)
        return shell_result(code, output, network)

    def _clone_source(self, url: str) -> tuple[str, Path | None, str]:
        """(url to clone, local repository to clone from or None, default destination) for a git_clone url."""
        if self.public_clone_only:
            from .clone import CloneRefused, public_https_url
            try:
                url = public_https_url(url)
            except CloneRefused as e:
                raise ToolError(str(e)) from e
            return url, None, url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if url.startswith("local:"):
            name = url[len("local:"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
                raise ToolError(f"bad local repository name: {name}")
            source = next((c for c in (self.repos_dir / name, self.repos_dir / f"{name}.git") if c.is_dir()), None)
            if source is None:
                available = sorted(p.name for p in self.repos_dir.iterdir()) if self.repos_dir.is_dir() else []
                raise ToolError(f"no local repository {name!r}; available: {', '.join(available) or 'none'}")
            return url, source, name.removesuffix(".git")
        if re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/[^\s'\"`$\\]+", url):
            return url, None, url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        raise ToolError("url must be https://... or local:<name>")

    async def _clone_public(self, url: str, target: Path, branch_args: list[str]) -> None:
        from .clone import QuotaExceeded, _run_clone
        from .projects import GitError
        cmd = ["git", "-c", "core.quotepath=off", "-c", "credential.helper=", "-c", "core.askPass=",
               "clone", "--config", "core.autocrlf=false", *branch_args, "--", url, str(target)]
        try:
            await asyncio.to_thread(_run_clone, cmd, target, timeout=600, max_bytes=self.clone_max_bytes)
        except (QuotaExceeded, GitError) as e:
            raise ToolError(str(e)) from e

    async def git_clone(self, url: str, dest: str | None = None, branch: str | None = None) -> str:
        url, source, default_dest = self._clone_source(url.strip())
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
        elif self.public_clone_only:
            await self._clone_public(url, target, branch_args)
            return f"cloned {url} into {rel}"
        else:
            cmd = " ".join(shlex.quote(a) for a in ["git", "clone", *branch_args, "--", url, rel])
            code, output = await self.sandbox.exec(cmd, timeout=600, network=True)
        if code != 0:
            raise ToolError(f"git clone failed (exit {code}): {truncate_middle(output.strip(), 2000)}")
        return f"cloned {url} into {rel}"

    def schemas(self) -> list[dict]:
        extra = []
        for kit in self.host_toolkits:
            extra.extend(kit.schemas())
        limits = self.tool_output
        return tool_schemas(self.read_lines, search_matches=limits.search_matches,
                            read_file_lines_max=limits.read_file_lines_max,
                            search_matches_max=limits.search_matches_max,
                            run_shell_chars=limits.run_shell_chars,
                            run_shell_chars_max=limits.run_shell_chars_max) + extra

    async def verify(self) -> ToolOutput:
        async def exec_cmd(command: str, timeout: int):
            return await self.sandbox.exec(command, timeout=timeout, network=False)
        return await run_verify(self.verify_checks, exec_cmd, self.tool_output.verify_summary_chars)

    async def call(self, name: str, args: dict) -> str:
        for kit in self.host_toolkits:
            if name in kit.tool_names:
                return await kit.call(name, args)
        if name == "verify":
            return await self.verify()
        if name in ("run_shell", "git_clone"):
            return await getattr(self, name)(**args)
        return await asyncio.to_thread(getattr(self.files, name), **args)
