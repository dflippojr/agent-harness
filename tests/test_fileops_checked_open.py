"""FileOps opens the file it checked: reads and writes go through a descriptor that is verified after the open."""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from harness import fileops
from harness.fileops import FileOps, ToolError, write_text_within


def _dir_link(link: Path, target: Path) -> None:
    """A directory link at `link` to `target`: a junction on Windows (no privilege needed), else a symlink."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def layout(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("TOKEN=outside\n", encoding="utf-8")
    return ws, outside


def _swap_sub_for_link(ws: Path, outside: Path) -> None:
    """ws/sub becomes a directory link to `outside`, as if it changed after its path was checked."""
    (ws / "sub" / "a.txt").unlink()
    (ws / "sub").rmdir()
    try:
        _dir_link(ws / "sub", outside)
    except OSError:
        pytest.skip("this account can't create directory links")


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


def test_ordinary_files_read_write_edit_and_search_as_before(tmp_path):
    files = FileOps(tmp_path, 8000)
    assert files.write_file("deep/new/a.txt", "one\r\ntwo\n") == "wrote 9 characters to deep/new/a.txt"
    assert (tmp_path / "deep" / "new" / "a.txt").read_bytes() == b"one\r\ntwo\n"
    assert files.read_file("deep/new/a.txt") == "1\tone\n2\ttwo"
    assert files.search("tw") == "deep/new/a.txt:2: two"
    assert files.edit_file("deep/new/a.txt", "two", "three") == "edited deep/new/a.txt"
    assert (tmp_path / "deep" / "new" / "a.txt").read_bytes() == b"one\nthree\n"
    assert files.write_bytes("deep/new/b.bin", b"\x00\xff") == "wrote 2 bytes to deep/new/b.bin"
    assert (tmp_path / "deep" / "new" / "b.bin").read_bytes() == b"\x00\xff"
    assert _names(tmp_path / "deep" / "new") == ["a.txt", "b.bin"]  # no temp file left behind
    with pytest.raises(ToolError, match="no such file"):
        files.read_file("deep")
    with pytest.raises(ToolError, match="no such file"):
        files.read_file("missing.txt")
    with pytest.raises(ToolError, match="not a file"):
        files.write_file(".", "x")


def test_a_failed_text_write_leaves_the_file_untouched(tmp_path):
    (tmp_path / "a.txt").write_text("keep\n", encoding="utf-8")
    files = FileOps(tmp_path, 8000)
    with pytest.raises(UnicodeEncodeError):
        files.write_file("a.txt", "half \ud800")
    with pytest.raises(UnicodeEncodeError):
        files.edit_file("a.txt", "keep", "\ud800")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "keep\n"
    assert _names(tmp_path) == ["a.txt"]


def test_a_hard_link_to_a_file_outside_is_not_read(layout):
    ws, outside = layout
    os.link(outside / "a.txt", ws / "linked.txt")
    files = FileOps(ws, 8000)
    with pytest.raises(ToolError, match="hard links"):
        files.read_file("linked.txt")
    assert files.search("TOKEN") == "no matches"
    assert files.search("TOKEN", path="linked.txt") == "no matches"
    with pytest.raises(ToolError, match="hard links"):
        files.edit_file("linked.txt", "TOKEN", "CHANGED")
    assert files.preview_diff("edit_file", {"path": "linked.txt", "old_text": "TOKEN", "new_text": "X"}) == ""
    assert (outside / "a.txt").read_text(encoding="utf-8") == "TOKEN=outside\n"


def test_writing_over_a_hard_link_replaces_the_name_instead_of_writing_through(layout):
    ws, outside = layout
    os.link(outside / "a.txt", ws / "linked.txt")
    os.link(outside / "a.txt", ws / "linked.bin")
    files = FileOps(ws, 8000)
    files.write_file("linked.txt", "new\n")
    files.write_bytes("linked.bin", b"new")
    assert (ws / "linked.txt").read_text(encoding="utf-8") == "new\n"
    assert (ws / "linked.bin").read_bytes() == b"new"
    assert (outside / "a.txt").read_text(encoding="utf-8") == "TOKEN=outside\n"
    assert files.read_file("linked.txt") == "1\tnew"  # a single link again, so it reads


def test_a_directory_swapped_for_a_link_after_the_check_is_not_opened(layout):
    ws, outside = layout
    files = FileOps(ws, 8000)
    checked = files.resolve("sub/a.txt")
    _swap_sub_for_link(ws, outside)
    with pytest.raises(ToolError, match="escapes the workspace"):
        files.read_checked(checked, "sub/a.txt")
    with pytest.raises(ToolError, match="escapes the workspace"):
        files.write_replacing(checked, "sub/a.txt", b"overwritten\n")
    assert (outside / "a.txt").read_text(encoding="utf-8") == "TOKEN=outside\n"
    assert _names(outside) == ["a.txt"]  # the new file was not left there either


def test_without_a_descriptor_path_the_checked_path_must_still_name_the_opened_file(layout, monkeypatch):
    ws, outside = layout
    monkeypatch.setattr(fileops, "_descriptor_path", lambda fd: None)
    files = FileOps(ws, 8000)
    assert files.read_file("sub/a.txt") == "1\tinside"
    files.write_file("sub/b.txt", "b\n")
    assert (ws / "sub" / "b.txt").read_text(encoding="utf-8") == "b\n"
    (ws / "sub" / "b.txt").unlink()
    checked = files.resolve("sub/a.txt")
    _swap_sub_for_link(ws, outside)
    with pytest.raises(ToolError, match="escapes the workspace"):
        files.read_checked(checked, "sub/a.txt")


def test_search_reads_the_files_it_listed_through_a_checked_open(layout, monkeypatch):
    ws, outside = layout
    files = FileOps(ws, 8000)
    listed = files._walk_files

    def walk_then_swap(base):
        found = listed(base)
        _swap_sub_for_link(ws, outside)
        return found
    monkeypatch.setattr(files, "_walk_files", walk_then_swap)
    assert files.search("TOKEN|inside") == "no matches"


def test_a_directory_link_that_stays_inside_still_reads_and_writes_through(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_text("hello\n", encoding="utf-8")
    try:
        _dir_link(tmp_path / "alias", tmp_path / "real")
    except OSError:
        pytest.skip("this account can't create directory links")
    files = FileOps(tmp_path, 8000)
    assert files.read_file("alias/a.txt") == "1\thello"
    files.edit_file("alias/a.txt", "hello", "edited")
    files.write_file("alias/b.txt", "b\n")
    assert (tmp_path / "real" / "a.txt").read_text(encoding="utf-8") == "edited\n"
    assert (tmp_path / "real" / "b.txt").read_text(encoding="utf-8") == "b\n"
    assert Path(os.path.realpath(tmp_path / "alias")) == Path(os.path.realpath(tmp_path / "real"))


def test_a_file_symlink_that_stays_inside_still_reads_searches_and_writes_through(tmp_path):
    (tmp_path / "target.txt").write_text("hello\n", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    except OSError:
        pytest.skip("this account can't create symlinks")
    files = FileOps(tmp_path, 8000)
    assert files.read_file("link.txt") == "1\thello"
    assert "link.txt:1: hello" in files.search("hello").splitlines()
    files.write_file("link.txt", "new\n")
    assert (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "new\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_in_the_workspace_is_skipped_without_waiting(tmp_path):
    (tmp_path / "a.txt").write_text("hit\n", encoding="utf-8")
    os.mkfifo(tmp_path / "pipe")
    files = FileOps(tmp_path, 8000)
    out: dict = {}

    def run():
        out["search"] = files.search("hit")
        try:
            files.read_file("pipe")
        except ToolError as e:
            out["read"] = str(e)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert out == {"search": "a.txt:1: hit", "read": "no such file: pipe"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_replacing_a_file_keeps_its_permission_bits(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("echo one\n", encoding="utf-8")
    script.chmod(0o750)
    FileOps(tmp_path, 8000).write_file("run.sh", "echo two\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o750
    assert script.read_text(encoding="utf-8") == "echo two\n"


def test_containment_compares_identity_when_the_root_is_spelled_differently(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    spelled = Path("\\\\?\\" + str(root)) if sys.platform == "win32" else Path("//" + str(root).lstrip("/"))
    assert str(spelled) != str(root)
    assert fileops._within(root, spelled / "a.txt")
    assert not fileops._within(root, tmp_path / "b.txt")
    assert not fileops._within(root, tmp_path / "missing" / "a.txt")


def test_windows_final_paths_lose_their_extended_prefix():
    assert str(fileops._plain_windows_path("\\\\?\\C:\\ws\\a.txt")) == str(Path("C:\\ws\\a.txt"))
    assert str(fileops._plain_windows_path("\\\\?\\UNC\\host\\share\\a.txt")) == str(Path("\\\\host\\share\\a.txt"))
    assert str(fileops._plain_windows_path("C:\\ws\\a.txt")) == str(Path("C:\\ws\\a.txt"))


def test_descriptor_paths_come_from_f_getpath_or_proc(monkeypatch):
    class Fcntl:
        F_GETPATH = 50

        def __init__(self, raw):
            self.raw = raw

        def fcntl(self, fd, op, arg):
            assert op == self.F_GETPATH and len(arg) == 1024
            if isinstance(self.raw, OSError):
                raise self.raw
            return self.raw + b"\0" * (1024 - len(self.raw))
    monkeypatch.setattr(fileops, "_WINDOWS", False)
    monkeypatch.setattr(fileops, "fcntl", Fcntl(b"/Users/me/ws/a.txt"))
    assert fileops._descriptor_path(5) == Path("/Users/me/ws/a.txt")
    monkeypatch.setattr(fileops, "fcntl", Fcntl(OSError(errno.EBADF, "bad descriptor")))
    assert fileops._descriptor_path(5) is None
    monkeypatch.setattr(fileops, "fcntl", None)
    links = {"/proc/self/fd/5": "/srv/ws/a.txt", "/proc/self/fd/6": "pipe:[1234]"}

    def readlink(path):
        if path not in links:
            raise FileNotFoundError(path)
        return links[path]
    monkeypatch.setattr(os, "readlink", readlink)
    assert fileops._descriptor_path(5) == Path("/srv/ws/a.txt")
    assert fileops._descriptor_path(6) is None  # not a file on disk
    assert fileops._descriptor_path(7) is None  # no /proc


def _failing_open(monkeypatch, path: Path, err: OSError) -> None:
    real_open = os.open

    def fake(p, *args, **kwargs):
        if Path(p) == path:
            raise err
        return real_open(p, *args, **kwargs)
    monkeypatch.setattr(os, "open", fake)


def test_an_open_that_meets_a_symlink_is_refused_and_other_errors_surface(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    files = FileOps(tmp_path, 8000)
    _failing_open(monkeypatch, files.root / "a.txt", OSError(errno.ELOOP, "too many levels of symbolic links"))
    with pytest.raises(ToolError, match="escapes the workspace"):
        files.read_file("a.txt")
    _failing_open(monkeypatch, files.root / "a.txt", PermissionError(errno.EACCES, "denied"))
    with pytest.raises(PermissionError):
        files.read_file("a.txt")


def test_opening_the_target_directory_refuses_a_symlink_and_surfaces_other_errors(tmp_path, monkeypatch):
    _failing_open(monkeypatch, tmp_path, OSError(errno.ELOOP, "too many levels of symbolic links"))
    with pytest.raises(ToolError, match="refused"):
        fileops._open_dir(tmp_path, "refused")
    _failing_open(monkeypatch, tmp_path, OSError(errno.ENOTDIR, "not a directory"))
    with pytest.raises(ToolError, match="refused"):
        fileops._open_dir(tmp_path, "refused")
    _failing_open(monkeypatch, tmp_path, PermissionError(errno.EACCES, "denied"))
    with pytest.raises(PermissionError):
        fileops._open_dir(tmp_path, "refused")


@pytest.mark.skipif(sys.platform == "win32", reason="the whole path would pass Windows' 260-character limit")
def test_a_file_whose_name_is_at_the_length_limit_can_still_be_replaced(tmp_path):
    name = "n" * 251 + ".txt"
    (tmp_path / name).write_text("one\n", encoding="utf-8")
    files = FileOps(tmp_path, 8000)
    files.write_file(name, "two\n")
    files.edit_file(name, "two", "three")
    assert (tmp_path / name).read_text(encoding="utf-8") == "three\n"
    assert _names(tmp_path) == [name]


def test_write_text_within_refuses_a_directory_swapped_after_the_check(layout, monkeypatch):
    ws, outside = layout
    replace = fileops._replace_within

    def swap_then_replace(*args):
        _swap_sub_for_link(ws, outside)
        return replace(*args)
    monkeypatch.setattr(fileops, "_replace_within", swap_then_replace)
    with pytest.raises(ToolError, match="outside"):
        write_text_within(ws, ws / "sub" / "a.txt", "overwritten\n")
    assert (outside / "a.txt").read_text(encoding="utf-8") == "TOKEN=outside\n"
    assert _names(outside) == ["a.txt"]
