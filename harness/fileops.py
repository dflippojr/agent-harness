"""File tools shared by the tower daemon and the MacBook runner.

Stdlib only and Python 3.9 compatible: the runner copies this file to the Mac, whose stock Python is 3.9.
"""

from __future__ import annotations

import contextlib
import difflib
import errno
import functools
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, NamedTuple

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_WINDOWS = sys.platform == "win32"
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # opening a FIFO for reading must not wait for a writer
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK}  # O_NOFOLLOW met a symlink (EMLINK on FreeBSD)
_DIR_FD = {os.open, os.stat, os.rename, os.unlink} <= os.supports_dir_fd
_KEEP_MODE = os.name == "posix"  # Windows has only a read-only flag, which a replace can't go through anyway

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
    tail = text[len(text) - half:]  # not text[-half:]: at limit 1, text[-0:] keeps it all
    return f"{text[:half]}\n... [{len(text) - limit} characters truncated] ...\n{tail}"


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


def _plain_windows_path(text: str) -> Path:
    r"""`text` without the `\\?\` or `\\?\UNC\` prefix that Windows' final-path calls return."""
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    return Path(text[4:]) if text.startswith("\\\\?\\") else Path(text)


def _windows_final_path(fd: int) -> Path | None:
    import ctypes
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return None
    final_path = _final_path_by_handle()
    size = final_path(handle, None, 0, 0)  # the length needed, terminator included; 0 on failure
    buf = ctypes.create_unicode_buffer(max(size, 1))
    # Success returns the length without the terminator; a path that grew in between returns a larger size.
    return _plain_windows_path(buf.value) if size and 0 < final_path(handle, buf, size, 0) < size else None


def _descriptor_path(fd: int) -> Path | None:
    """The real path of the file or directory open on `fd`, or None where the platform can't say."""
    if _WINDOWS:
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
    r"""`p` is `root` or below it: by name, or else by the identity of `p` or one of its parents, which still
    matches when the two are spelled differently (letter case on a case-insensitive disk, a `\\?\` prefix). A file
    system without file ids (st_ino 0) can't be compared by identity."""
    if p == root or p.is_relative_to(root):
        return True
    try:
        want = os.lstat(root)
        return bool(want.st_ino) and any(os.path.samestat(want, os.lstat(q)) for q in (p, *p.parents))
    except OSError:
        return False


def _opened_inside(root: Path, fd: int, path: Path) -> bool:
    """Whether what is open on `fd`, opened by the resolved `path`, is inside `root`. Uses the descriptor's own
    path; where the platform has none, `path` must still resolve to itself and name the same file."""
    real = _descriptor_path(fd)
    if real is None:
        try:
            same = os.path.samestat(os.fstat(fd), os.lstat(path))
        except OSError:
            same = False
        real = path if same and resolve_path(path) == path else None
    return real is not None and _within(root, real)


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


def _open_dir(path: Path, refusal: str) -> int:
    """A descriptor for the directory at `path`; a symlink there raises ToolError(`refusal`)."""
    try:
        return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as e:
        if e.errno in _LINK_ERRNOS or e.errno == errno.ENOTDIR:
            raise ToolError(refusal) from None
        raise


def _mode_to_keep(name, at: dict) -> int | None:
    """Permission bits of the regular file at `name` (relative to `at`'s dir_fd, if any), or None."""
    try:
        st = os.stat(name, follow_symlinks=False, **at)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else None


def _replace_within(root: Path, dest: Path, data: bytes, refusal: str) -> None:
    """Replace the file at the resolved path `dest` (inside the resolved `root`) with `data`.

    The bytes go to a new O_EXCL file beside `dest` that is then renamed over it, so a link at `dest` is replaced
    rather than written through and a failed write leaves the old file whole. Before any bytes go in, the new
    file's own descriptor must be inside `root`, which checks the directory it landed in. Where the platform
    allows, that directory is opened once (refusing a symlink) and the new file and the rename both go through
    that descriptor. An existing file keeps its permission bits. Raises ToolError(`refusal`) when the check fails.
    """
    tmp = f".{dest.name[:32]}.{os.getpid()}-{os.urandom(4).hex()}.tmp"  # dest's own name may be at the length limit
    dfd = _open_dir(dest.parent, refusal) if _DIR_FD else None
    at = {} if dfd is None else {"dir_fd": dfd}
    new, old = (dest.parent / tmp, dest) if dfd is None else (tmp, dest.name)
    try:
        mode = _mode_to_keep(old, at) if _KEEP_MODE else None
        fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o666, **at)
        try:
            try:
                if not _opened_inside(root, fd, dest.parent / tmp):
                    raise ToolError(refusal)
                if mode is not None:
                    os.fchmod(fd, mode)
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(new, old, **({} if dfd is None else {"src_dir_fd": dfd, "dst_dir_fd": dfd}))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(new, **at)
            raise
    finally:
        if dfd is not None:
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


_O_PATH = getattr(os, "O_PATH", 0)  # Linux: a descriptor for an entry, which needs no permission on the entry itself
# Without O_PATH (macOS), a directory the owner can't read can't be pinned, so it is changed by name instead. That is
# safe only where chmod can leave a link alone (follow_symlinks=False with dir_fd); elsewhere the directory is left.
_CHMOD_NOFOLLOW = os.chmod in os.supports_dir_fd and os.chmod in os.supports_follow_symlinks


def _is_reparse_point(path: str) -> bool:
    """A symlink, or on Windows a junction or other reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class _FdTreeOps:
    """The operations of the owner-access walk, through descriptors. A handle is an open file descriptor.

    Each entry is pinned with O_NOFOLLOW before it is read or changed, and both happen through the descriptor, so a
    name swapped for a link after the walk listed it can't redirect the change. Used wherever POSIX descriptors work.
    """

    stat = staticmethod(os.fstat)
    listdir = staticmethod(os.listdir)
    close = staticmethod(os.close)

    @property
    def chmod_by_name(self) -> bool:
        return _CHMOD_NOFOLLOW

    def open_parent(self, path: Path) -> int:
        return os.open(path.parent, os.O_RDONLY | _O_DIRECTORY)

    def pin(self, name: str, parent: int) -> int | None:
        """A descriptor for `name` that does not follow a link, or None where it can't be opened.

        O_NONBLOCK keeps a FIFO with no writer from stalling the walk: opening it returns at once.
        """
        with contextlib.suppress(OSError):
            return os.open(name, (_O_PATH or os.O_RDONLY) | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=parent)
        return None

    def chmod(self, pinned: int, mode: int) -> None:
        # An O_PATH descriptor can't be passed to fchmod: its inode is changed through /proc, whatever the name it was
        # opened by has become.
        os.chmod(f"/proc/self/fd/{pinned}" if _O_PATH else pinned, mode)

    def stat_name(self, name: str, parent: int) -> os.stat_result:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)

    def chmod_name(self, name: str, parent: int, mode: int) -> None:
        os.chmod(name, mode, dir_fd=parent, follow_symlinks=False)

    def open_dir(self, name: str, parent: int) -> int:
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent)


class _PinnedPath(NamedTuple):
    path: str
    st: os.stat_result


class _PathTreeOps:
    """The same operations by path, for where descriptors can't do them (Windows). A directory handle is its path.

    An entry is pinned by what lstat saw: a link or junction is refused, and the entry is looked at again, and must be
    the same file and not a link, just before it is changed. Check and change are still two steps, so a swap in
    between is a residual race that descriptors don't have; on Windows chmod only toggles the read-only flag.
    """

    chmod_by_name = True   # what stat_name returned is not a link, so a name is as good as a handle
    listdir = staticmethod(os.listdir)

    def open_parent(self, path: Path) -> str:
        parent = str(path.parent)
        if not os.path.isdir(parent):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), parent)
        return parent

    def pin(self, name: str, parent: str) -> _PinnedPath | None:
        full = os.path.join(parent, name)
        try:
            st = os.lstat(full)
        except OSError:
            return None
        return None if _is_reparse_point(full) else _PinnedPath(full, st)

    def stat(self, pinned: _PinnedPath) -> os.stat_result:
        return pinned.st

    def chmod(self, pinned: _PinnedPath, mode: int) -> None:
        if _is_reparse_point(pinned.path) or not os.path.samestat(pinned.st, os.lstat(pinned.path)):
            raise OSError(errno.EBUSY, "entry changed since it was looked at", pinned.path)
        os.chmod(pinned.path, mode)

    def stat_name(self, name: str, parent: str) -> os.stat_result:
        full = os.path.join(parent, name)
        st = os.lstat(full)
        if _is_reparse_point(full):
            raise OSError(errno.ELOOP, "a link or junction", full)
        return st

    def chmod_name(self, name: str, parent: str, mode: int) -> None:
        os.chmod(os.path.join(parent, name), mode)

    def open_dir(self, name: str, parent: str) -> str:
        full = os.path.join(parent, name)
        if _is_reparse_point(full) or not os.path.isdir(full):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), full)
        return full

    def close(self, handle: str) -> None:
        pass


# What the owner-access walk needs of a tree, which `_FdTreeOps` and `_PathTreeOps` each provide. A handle is a
# descriptor or a path, whichever the implementation works in, so the walk takes the operations as `Any`:
#   open_parent(path) -> handle of the directory `path` is in; raises OSError where there is none
#   pin(name, parent) -> handle for the entry `name` that does not follow a link; None where it can't be had
#   stat(pinned) -> the pinned entry's stat;  chmod(pinned, mode) -> set its mode, raising OSError where it can't
#   stat_name(name, parent) -> the stat of `name` itself, not of what a link points to; raises OSError for a link or a
#       missing name;  chmod_name(name, parent, mode) -> set its mode without following a link
#   chmod_by_name -> whether chmod_name may be used;  open_dir(name, parent) -> handle for the directory `name`,
#       raising OSError for a link or a non-directory;  listdir(directory) -> its names;  close(handle)
_TreeOps = Any


def _tree_ops() -> _FdTreeOps | _PathTreeOps:
    """Descriptors where this platform has what the walk needs of them, else paths. POSIX never takes the path route."""
    if _DIR_FD and os.listdir in os.supports_fd and (_O_PATH or os.chmod in os.supports_fd):
        return _FdTreeOps()
    return _PathTreeOps()


def _grant_unpinned_dir(ops: _TreeOps, name: str, parent: Any) -> bool:
    """For an entry that can't be pinned: owner access by name, relative to its parent, without following a link.

    Returns whether `name` is a directory whose entries should be reached. Where chmod can't avoid links, nothing is
    changed and the directory stays as it was.
    """
    try:
        st = ops.stat_name(name, parent)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if ops.chmod_by_name:
        with contextlib.suppress(OSError):
            ops.chmod_name(name, parent, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
    return ops.chmod_by_name


def _grant_entry(ops: _TreeOps, name: str, parent: Any) -> None:
    """Owner access for the entry `name` in `parent`, then for everything under it if it is a directory."""
    pinned = ops.pin(name, parent)
    if pinned is None:
        is_dir = _grant_unpinned_dir(ops, name, parent)
    else:
        try:
            st = ops.stat(pinned)
            is_dir = stat.S_ISDIR(st.st_mode)
            if not stat.S_ISLNK(st.st_mode):
                with contextlib.suppress(OSError):
                    ops.chmod(pinned, stat.S_IMODE(st.st_mode) | (stat.S_IRWXU if is_dir else stat.S_IWUSR))
        except OSError:
            return
        finally:
            ops.close(pinned)
    if not is_dir:
        return
    try:
        child = ops.open_dir(name, parent)
    except OSError:
        return
    try:
        for entry in ops.listdir(child):
            _grant_entry(ops, entry, child)
    finally:
        ops.close(child)


def grant_tree_owner_access(path: Path) -> None:
    """Owner access for `path` and everything under it, without following a link or a junction.

    Where POSIX descriptors work, the walk goes through them: each entry is pinned with O_NOFOLLOW before it is changed,
    so a link swapped in during the walk is refused rather than followed. Elsewhere it goes by path, checking each name
    for a link or junction (see `_PathTreeOps`). One walk drives both.
    """
    ops: _TreeOps = _tree_ops()
    try:
        parent = ops.open_parent(path)
    except OSError:
        return
    try:
        _grant_entry(ops, path.name, parent)
    finally:
        ops.close(parent)


def remove_tree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files and directories with no owner access.

    Git objects are read-only, and a failed step can leave a directory that its owner can't list. Access is granted
    to the whole tree first, so the removal itself doesn't fail on permissions.
    """
    if not path.exists():
        return
    grant_tree_owner_access(path)
    shutil.rmtree(path)


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
