"""File tools shared by the tower daemon and the MacBook runner.

Stdlib only and Python 3.9 compatible: the runner copies this file to the Mac, whose stock Python is 3.9.
"""

from __future__ import annotations

import difflib
import errno
import functools
import os
import re
import stat
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # opening a FIFO for reading must not wait for a writer
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK}  # O_NOFOLLOW met a symlink (EMLINK on FreeBSD)
_DIR_FD = {os.open, os.stat, os.rename, os.unlink} <= os.supports_dir_fd

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
MAX_PUT_BYTES = 32 * 1024 * 1024  # binary files the daemon may send to a runner (ComfyUI PNGs are much smaller)
OUTPUT_CAP = 1_000_000  # characters of command output kept in the sandbox / Mac runner
CAPTURE_CAPPED_NOTE = f"[capture capped at {OUTPUT_CAP} characters per stream]"


class ToolError(Exception):
    pass


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{text[-half:]}"


def cap_command_output(text: str, limit: int = OUTPUT_CAP) -> str:
    """Hard capture ceiling so a runaway command cannot exhaust memory."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [output cut] ...\n{text[-half:]}"


class CappedStream:
    """Keep at most `limit` characters (head + tail) while still draining the pipe."""

    def __init__(self, limit: int = OUTPUT_CAP):
        self.limit = limit
        self.half = limit // 2
        self.total = 0
        self.capped = False
        self._buf: list[str] = []
        self._buf_len = 0
        self._head: str | None = None
        self._tail: list[str] = []
        self._tail_len = 0

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        if not self.capped:
            self._buf.append(chunk)
            self._buf_len += len(chunk)
            if self._buf_len > self.limit:
                self.capped = True
                joined = "".join(self._buf)
                self._head = joined[:self.half]
                tail = joined[-self.half:]
                self._tail = [tail]
                self._tail_len = len(tail)
                self._buf = []
                self._buf_len = 0
            return
        self._tail.append(chunk)
        self._tail_len += len(chunk)
        extra = self._tail_len - self.half
        while extra > 0 and self._tail:
            first = self._tail[0]
            if len(first) <= extra:
                self._tail.pop(0)
                self._tail_len -= len(first)
                extra -= len(first)
            else:
                self._tail[0] = first[extra:]
                self._tail_len -= extra
                extra = 0

    def text(self) -> str:
        if not self.capped:
            return "".join(self._buf)
        tail = "".join(self._tail)
        if len(tail) > self.half:
            tail = tail[-self.half:]
        return f"{self._head}\n... [output cut] ...\n{tail}"

    def get(self) -> str:
        body = self.text()
        if not self.capped:
            return body
        return f"{body}\n{CAPTURE_CAPPED_NOTE}"


def resolve_path(p: Path) -> Path:
    r"""Path.resolve() that never returns Windows' extended-length form. Python 3.10 sometimes yields
    `\\?\C:\...` while a directory in the path is being created, which breaks containment checks."""
    resolved = p.resolve()
    text = str(resolved)
    return Path(text[4:]) if text.startswith("\\\\?\\") else resolved


@functools.lru_cache(maxsize=None)
def _final_path_by_handle():
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    fn.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    fn.restype = wintypes.DWORD
    return fn


def _windows_final_path(fd: int) -> Path | None:
    import ctypes
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return None
    size = 512
    while True:
        buf = ctypes.create_unicode_buffer(size)
        n = _final_path_by_handle()(handle, buf, size, 0)
        if n == 0:
            return None
        if n < size:
            break
        size = n  # too small: n is the size needed, terminator included
    text = buf.value
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    return Path(text[4:]) if text.startswith("\\\\?\\") else Path(text)


def _descriptor_path(fd: int) -> Path | None:
    """The real path of the file or directory open on `fd`, or None where the platform can't say."""
    if sys.platform == "win32":
        return _windows_final_path(fd)
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):  # macOS
        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        except OSError:
            return None
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    return Path(target) if target.startswith("/") else None


def _within(root: Path, p: Path) -> bool:
    """`p` is `root` or below it: by name, or else by the identity of `p` or one of its parents, which still
    matches when the two are spelled differently (letter case on a case-insensitive disk, a `\\\\?\\` prefix)."""
    if p == root or p.is_relative_to(root):
        return True
    try:
        want = os.lstat(root)
    except OSError:
        return False
    if not want.st_ino:  # a file system without file ids can't be compared this way
        return False
    for q in (p, *p.parents):
        try:
            st = os.lstat(q)
        except OSError:
            return False
        if (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino):
            return True
    return False


def _opened_inside(root: Path, fd: int, path: Path) -> bool:
    """Whether what is open on `fd`, opened by the resolved `path`, is inside `root`. Uses the descriptor's own
    path; where the platform has none, `path` must still resolve to itself and name the same file."""
    real = _descriptor_path(fd)
    if real is None:
        try:
            opened, named = os.fstat(fd), os.lstat(path)
        except OSError:
            return False
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or resolve_path(path) != path:
            return False
        real = path
    return _within(root, real)


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _replace_within(root: Path, dest: Path, data: bytes, refusal: str) -> None:
    """Replace the file at the resolved path `dest` (inside the resolved `root`) with `data`.

    The bytes go to a new O_EXCL file beside `dest` that is then renamed over it, so a link at `dest` is replaced
    rather than written through and a failed write leaves the old file whole. The directory the new file lands in
    is checked to be inside `root` after it is opened: as a descriptor that the new file and the rename then go
    through where the platform allows, else through the new file's own descriptor. An existing file keeps its
    permission bits. Raises ToolError(`refusal`) when the check fails.
    """
    tmp = f".{dest.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
    if not _DIR_FD:
        path = dest.parent / tmp
        fd = os.open(path, flags, 0o666)
        try:
            try:
                if not _opened_inside(root, fd, path):
                    raise ToolError(refusal)
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(path, dest)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return
    try:
        dfd = os.open(dest.parent, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as e:
        if e.errno in _LINK_ERRNOS or e.errno == errno.ENOTDIR:
            raise ToolError(refusal) from None
        raise
    try:
        if not _opened_inside(root, dfd, dest.parent):
            raise ToolError(refusal)
        try:
            old = os.stat(dest.name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        fd = os.open(tmp, flags, 0o666, dir_fd=dfd)
        try:
            try:
                if old is not None and stat.S_ISREG(old.st_mode):
                    os.fchmod(fd, stat.S_IMODE(old.st_mode))
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, dest.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dfd)


def write_text_within(root: Path, target: Path, text: str) -> Path:
    """Replace the file `target` inside `root` with `text` (UTF-8, platform line endings) and return the resolved
    path written.

    `target` is resolved first and must land inside the resolved `root`, so neither `..` nor a symlink can move
    the write elsewhere. The text goes to a new file beside the target that then replaces it, so a hard link at
    the target is broken rather than written through, and a failed write leaves the old file whole.
    """
    root_r = resolve_path(root)
    dest = resolve_path(target)
    refusal = f"{target} is outside {root}"
    if dest == root_r or not dest.is_relative_to(root_r):
        raise ToolError(refusal)
    data = text.replace("\n", os.linesep).encode("utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _replace_within(root_r, dest, data, refusal)
    return dest


def normalize_path(path: str | None, prefixes: tuple[str, ...] = ("/workspace",)) -> str:
    """Workspace-relative form of a path. Absolute paths under one of `prefixes` (the workspace root as the
    agent sees it) are made relative."""
    path = (path or ".").strip().replace("\\", "/")
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            path = path[len(prefix):]
            break
    return path.lstrip("/") or "."


def dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class FileOps:
    def __init__(self, root: Path, context_tokens: int, prefixes: tuple[str, ...] = ("/workspace",),
                 read_lines: int = 400, read_lines_max: int = 2000,
                 search_matches: int = 100, search_matches_max: int = 500):
        self.root = resolve_path(root)
        self.prefixes = prefixes
        self.read_lines = read_lines
        self.read_lines_max = read_lines_max
        self.search_matches = search_matches
        self.search_matches_max = search_matches_max
        # Size reads to the context window: short pages made Qwen answer from page 1 in Phase 0.
        self.read_chars = max(8000, int(context_tokens * 0.25 * 3.5))

    def _page_lines(self, max_lines=None) -> int:
        if max_lines is None:
            return max(1, self.read_lines)
        try:
            requested = int(max_lines)
        except (TypeError, ValueError):
            return max(1, self.read_lines)
        return max(1, min(requested, self.read_lines_max))

    def _match_limit(self, max_matches=None) -> int:
        if max_matches is None:
            return max(1, self.search_matches)
        try:
            requested = int(max_matches)
        except (TypeError, ValueError):
            return max(1, self.search_matches)
        return max(1, min(requested, self.search_matches_max))

    def resolve(self, path: str | None) -> Path:
        rel = normalize_path(path, self.prefixes)
        candidate = resolve_path(self.root / rel)
        if not self.contains(candidate):
            raise ToolError(f"path escapes the workspace: {path}")
        return candidate

    def contains(self, p: Path) -> bool:
        return p == self.root or p.is_relative_to(self.root)

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def _open_checked(self, p: Path, shown: str) -> int:
        """A read descriptor for the resolved path `p`, returned only once the file it opened is shown to be a
        regular file inside the workspace with no other hard link (whose other name could be anywhere)."""
        try:
            fd = os.open(p, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | _O_NONBLOCK)
        except OSError as e:
            if e.errno in _LINK_ERRNOS:
                raise ToolError(f"path escapes the workspace: {shown}") from None
            if not p.is_file():
                raise ToolError(f"no such file: {shown}") from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ToolError(f"no such file: {shown}")
            if not _opened_inside(self.root, fd, p):
                raise ToolError(f"path escapes the workspace: {shown}")
            if st.st_nlink > 1:
                raise ToolError(f"not opened: {shown} has other hard links")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read_checked(self, p: Path, shown: str, errors: str = "strict") -> str:
        """UTF-8 text (universal newlines) of the resolved path `p`, read through a checked descriptor."""
        fd = self._open_checked(p, shown)
        try:
            data = _read_all(fd)
        finally:
            os.close(fd)
        return data.decode("utf-8", errors).replace("\r\n", "\n").replace("\r", "\n")

    def write_replacing(self, p: Path, shown: str, data: bytes) -> None:
        """Replace the file at the resolved path `p` with `data`, creating its parent directories."""
        if p == self.root:
            raise ToolError(f"not a file: {shown}")
        p.parent.mkdir(parents=True, exist_ok=True)
        _replace_within(self.root, p, data, f"path escapes the workspace: {shown}")

    def _inside(self, p: Path) -> bool:
        """For entries found while walking: a symlink pointing out of the workspace is skipped."""
        return not p.is_symlink() or self.contains(resolve_path(p))

    def _walk(self, d: Path, depth: int, max_depth: int, entries: list[str]) -> None:
        for child in sorted(d.iterdir()):
            if child.name in SKIP_DIRS or len(entries) >= 500 or not self._inside(child):
                continue
            entries.append(self.rel(child) + ("/" if child.is_dir() else ""))
            if child.is_dir() and not child.is_symlink() and depth < max_depth:
                self._walk(child, depth + 1, max_depth, entries)

    def list_files(self, path: str = ".", max_depth: int = 2) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries: list[str] = []
        self._walk(base, 1, max_depth, entries)
        suffix = "\n... (listing truncated at 500 entries)" if len(entries) >= 500 else ""
        return "\n".join(entries) + suffix if entries else "(empty directory)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None, max_lines=None) -> str:
        p = self.resolve(path)
        lines = self.read_checked(p, path, errors="replace").splitlines()
        start = max(1, start_line)
        page = self._page_lines(max_lines)
        end = min(len(lines), end_line or len(lines), start + page - 1)
        body, shown_end = [], start - 1
        size = 0
        for n in range(start, end + 1):
            line = f"{n}\t{lines[n - 1]}"
            if size + len(line) > self.read_chars and body:
                break
            body.append(line)
            size += len(line) + 1
            shown_end = n
        text = "\n".join(body)
        if shown_end < len(lines):
            text += f"\n... ({len(lines)} lines total; continue with start_line={shown_end + 1})"
        return text or "(empty file)"

    def _walk_files(self, base: Path) -> list[Path]:
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):  # doesn't follow directory symlinks
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if self._inside(p):
                    found.append(p)
        return found

    def search(self, pattern: str, path: str = ".", offset: int = 0, max_matches=None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        try:
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            skip = 0
        limit = self._match_limit(max_matches)
        base = self.resolve(path)
        files = [base] if base.is_file() else self._walk_files(base)
        matches: list[str] = []
        seen = 0
        stopped = False
        for f in files:
            try:
                text = self.read_checked(resolve_path(f) if f.is_symlink() else f, str(f))
            except (UnicodeDecodeError, OSError, ToolError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if not regex.search(line):
                    continue
                if seen < skip:
                    seen += 1
                    continue
                matches.append(f"{self.rel(f)}:{n}: {line[:300]}")
                seen += 1
                if len(matches) >= limit:
                    stopped = True
                    break
            if stopped:
                break
        if not matches:
            return "no matches"
        text = "\n".join(matches)
        if stopped:
            start = skip + 1
            end = skip + len(matches)
            text += f"\n... (showing matches {start}-{end} of at least {end}; continue with offset={end})"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        self.write_replacing(p, path, content.encode("utf-8"))
        return f"wrote {len(content)} characters to {self.rel(p)}"

    def write_bytes(self, path: str, data: bytes) -> str:
        if len(data) > MAX_PUT_BYTES:
            raise ToolError(f"file is {len(data)} bytes; limit is {MAX_PUT_BYTES}")
        p = self.resolve(path)
        self.write_replacing(p, path, data)
        return f"wrote {len(data)} bytes to {self.rel(p)}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self.resolve(path)
        text = self.read_checked(p, path)
        count = text.count(old_text)
        if count != 1:
            raise ToolError(f"old_text must appear exactly once, found {count} occurrences")
        self.write_replacing(p, path, text.replace(old_text, new_text).encode("utf-8"))
        return f"edited {self.rel(p)}"

    def preview_diff(self, name: str, args: dict) -> str:
        """Unified diff of what a write/edit would change, for approval requests."""
        try:
            p = self.resolve(args.get("path"))
            old = self.read_checked(p, str(args.get("path")), errors="replace") if p.is_file() else ""
        except ToolError:
            return ""
        if name == "write_file":
            new = args.get("content", "")
        elif name == "edit_file" and old.count(args.get("old_text", "")) == 1:
            new = old.replace(args["old_text"], args.get("new_text", ""))
        else:
            return ""
        rel = normalize_path(args.get("path"), self.prefixes)
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="")
        return truncate_middle("\n".join(diff), 12000)


FILE_TOOLS = ("list_files", "read_file", "search", "write_file", "edit_file")
