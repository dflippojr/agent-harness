"""The user's memory library for agents: read access to allowed categories, approved writes, and the profile.

The daemon keeps its own clone (`memory_library.clone_dir`) and refreshes it with `git pull --ff-only` at most every
`refresh_minutes`. Only files under `categories/<name>/` for the allowlisted categories exist as far as agents can
tell, plus the agent profile (`profile_path`, see below): the top-level index, inbox, source archives, cross-category
capsules, and every other category (health, emotions, finance, relationships, ...) are neither listed, searched,
read, nor written. Anything the tools return is data, not instructions.

Writes (Phase 7b, user decision: every write asks). `memory_edit` and `memory_write` never run without the user
approving a diff on the phone, and that's enforced here, not only by the policy: the change is applied only when the
tool call has an approved approval whose diff is exactly what the change would do now (if the file moved on in
between, the agent is told to propose it again). Then the daemon's clone commits it and pushes, so every machine gets
it on its next `git pull`. Approval requests flag added lines that look like sensitive details, because instructions
alone didn't stop models from copying them (Phase 0 memory suite).

The agent profile (Phase 7c) is a short curated file with what every session should know about the user. The
manager puts it in each new session's system prompt once (so the prompt prefix stays cacheable), and agents propose
changes to it like any other memory write; they apply from the next session.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from pathlib import Path

from .config import MemoryLibraryConfig
from .fileops import ToolError, truncate_middle
from .sandbox import run_cmd

log = logging.getLogger("harness.memory_library")

READ_TOOLS = ("memory_index", "memory_search", "memory_read")
WRITE_TOOLS = ("memory_edit", "memory_write")
TOOLS = READ_TOOLS + WRITE_TOOLS
TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}
MAX_READ_CHARS = 60_000
MAX_FILE_CHARS = 200_000
# Never prompt for credentials: a push that needs a login fails instead of hanging the daemon.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
# Added lines matching these get a warning on the approval request. A hint for the reviewer, not a filter.
SENSITIVE = [
    ("health", r"\b(diagnos\w*|medication|prescri\w+|dosage|mg\b|therap(y|ist)|symptom\w*|disorder|syndrome|"
               r"surgery|injur(y|ies)|doctor|clinic|hospital|mental health|depress\w*|anxiety|adhd|autis\w*)"),
    ("finance", r"(\$\s?\d|\b\d[\d,]*\s?(usd|dollars)\b|\b(salary|income|debt|loan|mortgage|credit score|"
                r"bank account|savings|net worth|401k|ira)\b)"),
    ("relationships", r"\b(dating|girlfriend|boyfriend|partner|breakup|broke up|divorce|ex-\w+|hookup)\b"),
    ("identity", r"\b(religio\w+|sexual\w*|orientation|pronouns|politic\w+|immigration)\b"),
    ("credentials", r"\b(password|passcode|api[_ ]?key|secret|token|ssn|social security)\b"),
]


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


def schemas(cfg: MemoryLibraryConfig) -> list[dict]:
    cats = ", ".join(cfg.categories)
    out = [
        _fn("memory_index", "List the files in the user's personal memory library that you may read (categories: "
                            f"{cats}), with each file's title. Use it for context about the user's projects, work, "
                            "home, and tastes. Read-only.", {}),
        _fn("memory_search", "Search the readable memory library for a regular expression (case-insensitive). "
                             "Returns up to 100 'path:line: text' matches. Newer dated entries win over older ones.", {
            "pattern": {"type": "string"},
        }, ["pattern"]),
        _fn("memory_read", "Read a memory library file by the path memory_index or memory_search showed.", {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line. Default 1."},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
        }, ["path"]),
    ]
    if cfg.writes:
        profile = f" or the agent profile ({cfg.profile_path})" if cfg.profile_path else ""
        out += [
            _fn("memory_edit", "Propose a change to a memory library file in the readable categories" + profile +
                               ": replace old_text, which must appear exactly once, with new_text. The user "
                               "reviews the diff and approves or denies it; approved changes are committed.", {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "summary": {"type": "string", "description": "One line saying what changes and why; the commit "
                                                             "message the user sees."},
            }, ["path", "old_text", "new_text", "summary"]),
            _fn("memory_write", "Propose creating a new memory library file (or replacing one) in a readable "
                                "category" + profile + ". Prefer memory_edit for changes to existing files. The "
                                "user approves the diff first.", {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "summary": {"type": "string", "description": "One line saying what changes and why."},
            }, ["path", "content", "summary"]),
        ]
    return out


def sensitive_hits(diff: str) -> list[str]:
    """Kinds of sensitive detail that lines added by a unified diff seem to mention."""
    added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    return [kind for kind, pattern in SENSITIVE if re.search(pattern, added, re.IGNORECASE)]


class MemoryLibrary:
    tool_names = TOOLS
    wants_session = True

    def __init__(self, cfg: MemoryLibraryConfig, db=None):
        self.cfg = cfg
        self.db = db                      # approvals are checked before any write
        self.root = Path(cfg.clone_dir)
        self._refreshed = 0.0
        self._lock = asyncio.Lock()
        self.refresh_error = ""
        self.last_commit: dict = {}

    def schemas(self) -> list[dict]:
        return schemas(self.cfg)

    # sync
    def refresh_soon(self) -> None:
        """Pull in the background if the clone is due, so the next session's profile is current. Never blocks."""
        if time.monotonic() - self._refreshed < self.cfg.refresh_minutes * 60 and self.root.is_dir():
            return
        try:
            task = asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:  # no event loop (CLI tools): skip
            return
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # errors are in refresh_error already

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            await self._refresh(force)

    async def _refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._refreshed < self.cfg.refresh_minutes * 60 and self.root.is_dir():
            return
        if not (self.root / ".git").is_dir():
            if not self.cfg.repo:
                raise ToolError("the memory library isn't configured (memory_library.repo)")
            self.root.parent.mkdir(parents=True, exist_ok=True)
            code, out, err = await run_cmd(["git", "clone", "-q", "--", self.cfg.repo, str(self.root)], timeout=300,
                                           env=GIT_ENV)
        else:
            code, out, err = await self._git("pull", "-q", "--ff-only", timeout=120)
        self._refreshed = time.monotonic()
        self.refresh_error = "" if code == 0 else (err or out).strip()[:300]
        if code != 0:
            log.warning("memory library refresh failed: %s", self.refresh_error)
            if not self.root.is_dir():
                raise ToolError(f"the memory library couldn't be cloned: {self.refresh_error}")

    async def _git(self, *args: str, timeout: float = 120) -> tuple[int, str, str]:
        return await run_cmd(["git", "-C", str(self.root), *args], timeout=timeout, env=GIT_ENV)

    # access control
    @property
    def profile_rel(self) -> str:
        return self.cfg.profile_path.strip().replace("\\", "/").lstrip("/")

    def allowed_files(self) -> list[Path]:
        files = []
        for cat in self.cfg.categories:
            base = self.root / "categories" / cat
            if base.is_dir():
                files += [f for f in base.rglob("*") if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES
                          and self._allowed(f)]
        profile = self.profile_file()
        if profile is not None and profile.is_file():
            files.append(profile)
        return sorted(files)

    def profile_file(self) -> Path | None:
        return self.root / self.profile_rel if self.profile_rel else None

    def _allowed(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            return False
        if self.profile_rel and "/".join(rel) == self.profile_rel:
            return True
        return (len(rel) >= 3 and rel[0] == "categories" and rel[1] in self.cfg.categories
                and not any(p.startswith(".") for p in rel) and path.suffix.lower() in TEXT_SUFFIXES)

    def _resolve(self, path: str, must_exist: bool = True) -> Path:
        rel = path.strip().replace("\\", "/").lstrip("/")
        target = (self.root / rel).resolve()
        if (must_exist and not target.is_file()) or target.is_dir() or not self._allowed(target):
            raise ToolError(f"{path} isn't a {'readable' if must_exist else 'writable'} memory library file; "
                            "use memory_index to see what is")
        return target

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    # read tools
    def memory_index(self) -> str:
        lines = []
        for f in self.allowed_files():
            title = ""
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                    if line.startswith("#"):
                        title = line.lstrip("#").strip()
                        break
            except OSError:
                pass
            lines.append(f"- {self._rel(f)}" + (f" — {title}" if title else ""))
        note = f"\n(note: the library couldn't be refreshed, so it may be stale: {self.refresh_error})" \
            if self.refresh_error else ""
        return ("\n".join(lines) or "No readable files.") + note

    def memory_search(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"bad regular expression: {e}")
        hits = []
        for f in self.allowed_files():
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self._rel(f)}:{n}: {line[:300]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n... (first 100 matches; narrow the pattern)"
        return "\n".join(hits) or "no matches"

    def memory_read(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        f = self._resolve(path)
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else len(lines))
        out, size = [], 0
        for n in range(start, end + 1):
            text = f"{n}: {lines[n - 1]}"
            if size + len(text) > MAX_READ_CHARS:
                out.append(f"... [stopped at line {n - 1} of {len(lines)}; read on with start_line={n}]")
                break
            out.append(text)
            size += len(text) + 1
        if not out:
            return "(empty file)" if not lines else f"start_line {start} is past the end ({len(lines)} lines)"
        if end < len(lines) and not out[-1].startswith("..."):
            out.append(f"... [{len(lines) - end} more lines]")
        return "\n".join(out)

    # writes
    def _proposal(self, name: str, args: dict) -> tuple[Path, str, str, str]:
        """(file, relative path, old text, new text) for a write, or ToolError."""
        f = self._resolve(args.get("path", ""), must_exist=(name == "memory_edit"))
        rel = self._rel(f)
        old = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        if name == "memory_edit":
            old_text = args.get("old_text", "")
            if not old_text:
                raise ToolError("old_text is empty")
            count = old.count(old_text)
            if count != 1:
                raise ToolError(f"old_text appears {count} times in {rel}; it must appear exactly once "
                                "(read the file and include more surrounding text)")
            new = old.replace(old_text, args.get("new_text", ""))
        else:
            if f.suffix.lower() not in TEXT_SUFFIXES:
                raise ToolError(f"only {', '.join(sorted(TEXT_SUFFIXES))} files can be written")
            new = args.get("content", "")
        if new == old:
            raise ToolError("the change makes no difference to the file")
        limit = self.cfg.profile_max_chars if rel == self.profile_rel else MAX_FILE_CHARS
        if len(new) > limit:
            raise ToolError(f"{rel} would be {len(new)} characters; the limit is {limit}" +
                            (" (the profile must stay short: move detail into a category file)"
                             if rel == self.profile_rel else ""))
        if not args.get("summary", "").strip():
            raise ToolError("summary is empty")
        return f, rel, old, new

    @staticmethod
    def _diff(rel: str, old: str, new: str) -> str:
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="")
        return "\n".join(diff)

    async def preview(self, name: str, args: dict) -> tuple[str, str]:
        """(detail for the approval card: summary, blank line, diff; extra reason text). ToolError when the change
        can't be applied, so the agent hears about it without bothering the user. Refreshes first so the diff is
        against current files."""
        async with self._lock:
            try:
                await self._refresh()
            except ToolError:
                pass
            _, rel, old, new = self._proposal(name, args)
        diff = self._diff(rel, old, new)
        hits = sensitive_hits(diff)
        warning = f"⚠ added lines may mention {', '.join(hits)} details" if hits else ""
        return f"{' '.join(args['summary'].split())}\n\n{truncate_middle(diff, 12000)}", warning

    async def apply(self, name: str, args: dict, session_id: str, call_id: str) -> str:
        approval = self.db.approval_for_call(session_id, call_id) if self.db is not None else None
        if approval is None or approval["status"] != "approved":
            raise ToolError("memory library changes need the user's approval, and this one wasn't approved")
        async with self._lock:
            await self._sync_for_write()
            f, rel, old, new = self._proposal(name, args)
            diff = self._diff(rel, old, new)
            approved_diff = approval["detail"].split("\n\n", 1)[-1]
            if truncate_middle(diff, 12000) != approved_diff:
                raise ToolError(f"{rel} changed after this change was proposed, so the approved diff no longer "
                                "matches. Read the file again and propose the change again.")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(new, encoding="utf-8")
            summary = " ".join(args["summary"].split())[:150]
            message = f"{summary}\n\nApproved by the user; agent-harness session {session_id}."
            for step in (("add", "--", rel), ("commit", "-q", "-m", message)):
                code, out, err = await self._git(*step)
                if code != 0:
                    await self._reset()
                    raise ToolError(f"git {step[0]} failed: {(err or out).strip()[:300]}")
            pushed, detail = await self._push()
            code, head, _ = await self._git("rev-parse", "--short", "HEAD")
            self.last_commit = {"head": head.strip(), "path": rel, "summary": summary, "at": time.time(),
                                "pushed": pushed}
            if not pushed:
                raise ToolError(f"the change to {rel} was approved but couldn't be pushed ({detail}), so it was not "
                                "saved. Tell the user; they can try again later.")
            return f"Saved: {rel} ({summary}), commit {head.strip()}, pushed."

    async def _sync_for_write(self) -> None:
        """Bring the clone to the remote's state, dropping anything local (a push that failed earlier)."""
        if not (self.root / ".git").is_dir():
            await self._refresh(force=True)
        await self._git("fetch", "-q", "origin", timeout=120)
        await self._reset()
        self._refreshed = time.monotonic()

    async def _reset(self) -> None:
        code, upstream, _ = await self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        target = upstream.strip() if code == 0 and upstream.strip() else "HEAD"
        await self._git("reset", "-q", "--hard", target)
        await self._git("clean", "-q", "-fd")

    async def _push(self) -> tuple[bool, str]:
        code, out, err = await self._git("push", "-q", timeout=180)
        if code == 0:
            return True, ""
        # Someone pushed in between: replay this one commit on top and try once more.
        code2, out2, err2 = await self._git("pull", "-q", "--rebase", timeout=180)
        if code2 != 0:
            await self._git("rebase", "--abort")
            await self._reset()
            return False, (err2 or out2 or err or out).strip()[:300]
        code, out, err = await self._git("push", "-q", timeout=180)
        if code != 0:
            await self._reset()
            return False, (err or out).strip()[:300]
        return True, ""

    # profile (7c)
    def profile_text(self) -> str:
        """The profile as it is in the clone now, capped; '' when there is none."""
        f = self.profile_file()
        if f is None or not f.is_file():
            return ""
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) > self.cfg.profile_max_chars:
            text = text[: self.cfg.profile_max_chars] + "\n[... profile cut at its size limit]"
        return text

    async def owner_write(self, path: str, content: str, summary: str) -> dict:
        """Agent Harness Web save: same git commit/push as an approved agent write. The owner is the approver."""
        if not self.cfg.writes:
            raise ToolError("memory library writes are disabled")
        args = {"path": path, "content": content, "summary": summary}
        async with self._lock:
            await self._sync_for_write()
            f, rel, _, new = self._proposal("memory_write", args)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(new, encoding="utf-8")
            message = f"{' '.join(summary.split())[:150]}\n\nSaved from Agent Harness Web Settings."
            for step in (("add", "--", rel), ("commit", "-q", "-m", message)):
                code, out, err = await self._git(*step)
                if code != 0:
                    await self._reset()
                    raise ToolError(f"git {step[0]} failed: {(err or out).strip()[:300]}")
            pushed, detail = await self._push()
            code, head, _ = await self._git("rev-parse", "--short", "HEAD")
            self.last_commit = {"head": head.strip(), "path": rel, "summary": " ".join(summary.split())[:150],
                                "at": time.time(), "pushed": pushed}
            if not pushed:
                raise ToolError(f"the change to {rel} was saved locally but couldn't be pushed ({detail})")
            return self.last_commit

    async def call(self, name: str, args: dict, session: dict | None = None, call_id: str = "") -> str:
        if name in WRITE_TOOLS:
            if not self.cfg.writes:
                raise ToolError("memory library writes are disabled")
            return await self.apply(name, args, (session or {}).get("id", ""), call_id)
        await self.refresh()
        return await asyncio.to_thread(getattr(self, name), **args)
