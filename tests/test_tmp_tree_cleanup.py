"""Trees the server removes go even when a step left them without owner access (#754).

The cleanup used to chmod every entry to 0o200 (`stat.S_IWRITE` on POSIX replaces the mode), so the next listing of
that directory failed, and the tree stayed under pytest's basetemp for pytest's own cleanup to trip over.

One walk grants owner access, over a small set of operations with two implementations: descriptors (POSIX) and paths
(Windows, and the stand-in everywhere else). The walk tests run through both wherever both can run.
"""

import errno
import os
import stat
import sys
import threading

import pytest

from harness import app_stores, clone, fileops, maintenance
from harness.fileops import grant_tree_owner_access
from macrunner import harness_runner

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
WINDOWS = sys.platform == "win32"
FD_AVAILABLE = isinstance(fileops._tree_ops(), fileops._FdTreeOps)


@pytest.fixture(params=["path", "fd"])
def tree_ops(request, monkeypatch):
    """The walk's operations, once as paths and once as descriptors (skipped where descriptors can't do the walk)."""
    if request.param == "fd" and not FD_AVAILABLE:
        pytest.skip("this platform has no descriptor-relative operations")
    ops = fileops._FdTreeOps() if request.param == "fd" else fileops._PathTreeOps()
    monkeypatch.setattr(fileops, "_tree_ops", lambda: ops)
    return ops


@pytest.fixture
def fd_ops(monkeypatch):
    if not FD_AVAILABLE:
        pytest.skip("this platform has no descriptor-relative operations")
    ops = fileops._FdTreeOps()
    monkeypatch.setattr(fileops, "_tree_ops", lambda: ops)
    return ops


def open_descriptors():
    """The descriptors this process has open, where the platform lists them (none on Windows).

    Listing them opens descriptors of its own; those are closed again before each name is checked, so they are not counted.
    """
    for directory in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(directory):
            open_now = set()
            for name in os.listdir(directory):
                try:
                    os.fstat(int(name))
                except OSError:
                    continue
                open_now.add(int(name))
            return open_now
    return set()


@pytest.fixture(autouse=True)
def no_descriptor_left_open():
    """A test closes every descriptor it opens, its own parent handles included, whether it passes or fails."""
    before = open_descriptors()
    yield
    leaked = open_descriptors() - before
    assert not leaked, f"descriptors left open: {sorted(leaked)}"


def link_to_dir(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def make_outside(tmp_path):
    """A directory beside the tree, which no walk of the tree may change (Windows has no directory modes to check)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x", encoding="utf-8")
    (outside / "keep").chmod(0o444)
    outside.chmod(0o500)
    return outside


def assert_untouched(outside):
    assert mode(outside / "keep") == 0o444
    assert WINDOWS or mode(outside) == 0o500


REMOVERS = [
    pytest.param(clone._remove_tree, id="clone"),
    pytest.param(maintenance.remove_tree, id="maintenance"),
    pytest.param(app_stores.remove_tree, id="app_stores"),
    pytest.param(harness_runner.remove_tree, id="macrunner"),
]


@posix_only
@pytest.mark.parametrize("remove", REMOVERS)
def test_a_tree_with_no_owner_access_is_removed(tmp_path, remove, tree_ops):
    tree = tmp_path / "tree"
    sealed = tree / "sealed"                 # no read or search bits: a listing fails
    sealed.mkdir(parents=True)
    (sealed / "file").write_text("x", encoding="utf-8")
    sealed.chmod(0o000)
    read_only = tree / "read_only"           # a git object store: read-only files in a read-only directory
    read_only.mkdir()
    (read_only / "object").write_text("x", encoding="utf-8")
    (read_only / "object").chmod(0o444)
    read_only.chmod(0o500)
    write_only = tree / "write_only"         # the mode the old cleanup left behind
    write_only.mkdir()
    write_only.chmod(0o200)

    remove(tree)

    assert not tree.exists()


@pytest.mark.parametrize("remove", REMOVERS)
def test_a_tree_of_read_only_files_is_removed(tmp_path, remove, tree_ops):
    tree = tmp_path / "tree"
    (tree / "objects" / "ab").mkdir(parents=True)
    (tree / "objects" / "ab" / "object").write_text("x", encoding="utf-8")
    (tree / "objects" / "ab" / "object").chmod(0o444)
    (tree / "top").write_text("x", encoding="utf-8")
    (tree / "top").chmod(0o444)

    remove(tree)

    assert not tree.exists()


@pytest.mark.parametrize("remove", REMOVERS)
def test_removing_a_link_leaves_what_it_points_to_alone(tmp_path, remove, tree_ops):
    outside = make_outside(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    link_to_dir(tree / "elsewhere", outside)

    remove(tree)

    assert not tree.exists()
    assert (outside / "keep").read_text(encoding="utf-8") == "x"
    assert_untouched(outside)


def test_granting_access_to_a_link_root_leaves_its_target_alone(tmp_path, tree_ops):
    outside = make_outside(tmp_path)
    link = tmp_path / "link"
    link_to_dir(link, outside)

    grant_tree_owner_access(link)

    assert_untouched(outside)


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows reparse point")
def test_a_junction_is_not_walked(tmp_path, tree_ops):
    import _winapi
    outside = make_outside(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(tree / "junction"))
    except OSError:
        pytest.skip("junctions unavailable")

    grant_tree_owner_access(tree)
    grant_tree_owner_access(tree / "junction")

    assert_untouched(outside)


def test_a_name_swapped_for_a_link_after_the_walk_listed_it_is_not_followed(tmp_path, monkeypatch, tree_ops):
    outside = make_outside(tmp_path)
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    real_pin = tree_ops.pin
    swapped = []

    def swap_then_pin(name, parent):
        if name == "sub" and not swapped:   # another process replaces the directory the walk has just listed
            swapped.append(name)
            (tree / "sub").rmdir()
            link_to_dir(tree / "sub", outside)
        return real_pin(name, parent)
    monkeypatch.setattr(tree_ops, "pin", swap_then_pin)

    grant_tree_owner_access(tree)

    assert swapped
    assert_untouched(outside)


def test_a_name_swapped_for_a_link_after_it_was_pinned_is_not_followed(tmp_path, monkeypatch, tree_ops):
    outside = make_outside(tmp_path)
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    real_chmod = tree_ops.chmod
    swapped = []

    calls = []

    def swap_then_chmod(pinned, new_mode):
        calls.append(pinned)
        if len(calls) == 2:                 # the second change is sub's (the first is the tree's)
            swapped.append(pinned)          # the entry changes between being looked at and being changed
            (tree / "sub").rmdir()
            link_to_dir(tree / "sub", outside)
        return real_chmod(pinned, new_mode)
    monkeypatch.setattr(tree_ops, "chmod", swap_then_chmod)

    grant_tree_owner_access(tree)

    assert swapped
    assert_untouched(outside)


def test_a_tree_whose_parent_is_missing_is_left_alone(tmp_path, tree_ops):
    grant_tree_owner_access(tmp_path / "missing" / "tree")


def test_a_tree_that_is_already_gone_is_left_alone(tmp_path, tree_ops):
    grant_tree_owner_access(tmp_path / "gone")
    fileops.remove_tree(tmp_path / "gone")


def test_an_entry_that_vanished_before_it_was_reached_is_skipped(tmp_path, tree_ops):
    fileops._grant_entry(tree_ops, "missing", tree_ops.open_parent(tmp_path / "x"))


def test_a_file_is_made_writable_and_a_directory_gets_owner_access(tmp_path, tree_ops):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "object").write_text("x", encoding="utf-8")
    (tree / "sub" / "object").chmod(0o444)
    (tree / "sub").chmod(0o500)

    grant_tree_owner_access(tree)

    assert mode(tree / "sub" / "object") & stat.S_IWUSR
    assert WINDOWS or mode(tree / "sub") & stat.S_IRWXU == stat.S_IRWXU   # Windows keeps only a read-only flag


# --- The shared walk, over operations that can fail in ways a real tree can't be made to ---

class FakeOps:
    """A tree in memory: {"dir": {"file": None, "sub": {...}}}. Records the order the walk goes in."""

    def __init__(self, tree, *, unpinned=(), unopenable=(), chmod_fails=(), stat_fails=(), name_stat_fails=(), by_name=False):
        self.tree = tree
        self.unpinned = set(unpinned)
        self.unopenable = set(unopenable)
        self.chmod_fails = set(chmod_fails)
        self.stat_fails = set(stat_fails)
        self.name_stat_fails = set(name_stat_fails)
        self.by_name = by_name
        self.events = []
        self.open_handles = set()

    def _node(self, handle):
        node = self.tree
        for part in [p for p in handle.split("/") if p]:
            node = node[part]
        return node

    def open_parent(self, path):
        self.open_handles.add("")
        return ""

    def pin(self, name, parent):
        full = f"{parent}/{name}"
        if name in self.unpinned:
            return None
        self.open_handles.add(full)
        return full

    def stat(self, pinned):
        if pinned.rsplit("/", 1)[-1] in self.stat_fails:
            raise OSError(errno.EIO, "stat failed")
        is_dir = isinstance(self._node(pinned), dict)
        return os.stat_result((stat.S_IFDIR | 0o000 if is_dir else stat.S_IFREG | 0o444, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def chmod(self, pinned, new_mode):
        self.events.append(("chmod", pinned, oct(stat.S_IMODE(new_mode))))
        if pinned.rsplit("/", 1)[-1] in self.chmod_fails:
            raise PermissionError(errno.EPERM, "denied")

    @property
    def chmod_by_name(self):
        return self.by_name

    def stat_name(self, name, parent):
        self.events.append(("stat_name", f"{parent}/{name}"))
        if name in self.name_stat_fails:
            raise OSError(errno.ENOENT, "gone")
        is_dir = isinstance(self._node(f"{parent}/{name}"), dict)
        return os.stat_result((stat.S_IFDIR | 0o000 if is_dir else stat.S_IFREG | 0o000, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def chmod_name(self, name, parent, new_mode):
        self.events.append(("chmod_name", f"{parent}/{name}", oct(stat.S_IMODE(new_mode))))
        if name in self.chmod_fails:
            raise PermissionError(errno.EPERM, "denied")

    def open_dir(self, name, parent):
        if name in self.unopenable:
            raise OSError(errno.EACCES, "denied")
        full = f"{parent}/{name}"
        self.open_handles.add(full)
        return full

    def listdir(self, directory):
        self.events.append(("list", directory))
        return list(self._node(directory))

    def close(self, handle):
        self.open_handles.discard(handle)


def walk(fake, root="tree"):
    fileops._grant_entry(fake, root, fake.open_parent(None))
    fake.close("")
    assert not fake.open_handles   # every handle the walk opened was closed
    return fake.events


def test_the_walk_grants_a_directory_before_it_lists_it_and_visits_every_entry():
    fake = FakeOps({"tree": {"a": None, "sub": {"b": None}}})

    events = walk(fake)

    assert events == [
        ("chmod", "/tree", "0o700"), ("list", "/tree"),
        ("chmod", "/tree/a", "0o644"), ("chmod", "/tree/sub", "0o700"), ("list", "/tree/sub"),
        ("chmod", "/tree/sub/b", "0o644"),
    ]


def test_a_chmod_that_fails_does_not_stop_the_walk():
    fake = FakeOps({"tree": {"a": None, "sub": {"b": None}}}, chmod_fails={"tree", "a"})

    events = walk(fake)

    assert ("list", "/tree") in events and ("chmod", "/tree/sub/b", "0o644") in events


def test_an_entry_that_cannot_be_looked_at_is_skipped():
    fake = FakeOps({"tree": {"a": None, "sub": {"b": None}}}, stat_fails={"a", "sub"})

    events = walk(fake)

    assert ("list", "/tree/sub") not in events and ("chmod", "/tree/a", "0o644") not in events


def test_a_directory_that_cannot_be_opened_is_not_listed():
    fake = FakeOps({"tree": {"sub": {"b": None}}}, unopenable={"sub"})

    events = walk(fake)

    assert ("list", "/tree/sub") not in events and ("list", "/tree") in events


def test_an_entry_that_cannot_be_pinned_is_reached_only_where_a_name_can_be_changed_safely():
    tree = {"tree": {"sealed": {"b": None}}}
    shut = FakeOps(tree, unpinned={"sealed"})
    open_ = FakeOps(tree, unpinned={"sealed"}, by_name=True)

    assert ("list", "/tree/sealed") not in walk(shut)
    assert ("chmod_name", "/tree/sealed", "0o700") not in shut.events
    events = walk(open_)
    assert ("chmod_name", "/tree/sealed", "0o700") in events and ("list", "/tree/sealed") in events


def test_an_unpinnable_name_that_is_gone_or_is_not_a_directory_is_not_entered():
    fake = FakeOps({"tree": {"file": None, "gone": None}}, unpinned={"file", "gone"}, name_stat_fails={"gone"}, by_name=True)

    events = walk(fake)

    assert not [e for e in events if e[0] in ("chmod_name", "list") and e[1] != "/tree"]


def test_a_name_chmod_that_fails_still_reaches_the_directory():
    fake = FakeOps({"tree": {"sealed": {"b": None}}}, unpinned={"sealed"}, chmod_fails={"sealed"}, by_name=True)

    assert ("list", "/tree/sealed") in walk(fake)


# --- Which operations are used where ---

def test_posix_uses_descriptors_and_windows_uses_paths():
    expected = fileops._PathTreeOps if WINDOWS else fileops._FdTreeOps

    assert type(fileops._tree_ops()) is expected


def test_descriptors_are_not_used_where_they_cannot_do_the_walk(monkeypatch):
    monkeypatch.setattr(fileops, "_DIR_FD", False)

    assert type(fileops._tree_ops()) is fileops._PathTreeOps


@posix_only
def test_on_posix_the_walk_never_takes_the_path_route(tmp_path, monkeypatch):
    used = []
    for cls in (fileops._FdTreeOps, fileops._PathTreeOps):
        real = cls.pin
        monkeypatch.setattr(cls, "pin", lambda self, name, parent, _real=real: used.append(type(self)) or _real(self, name, parent))
    (tmp_path / "tree" / "sub").mkdir(parents=True)

    grant_tree_owner_access(tmp_path / "tree")

    assert used and set(used) == {fileops._FdTreeOps}


# --- The path-backed operations ---

def test_the_path_ops_refuse_a_parent_that_is_not_a_directory(tmp_path):
    (tmp_path / "file").write_text("x", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        fileops._PathTreeOps().open_parent(tmp_path / "file" / "tree")


def test_the_path_ops_pin_a_file_and_not_a_missing_name_or_a_link(tmp_path):
    ops = fileops._PathTreeOps()
    (tmp_path / "file").write_text("x", encoding="utf-8")
    link_to_dir(tmp_path / "link", tmp_path)
    parent = ops.open_parent(tmp_path / "file")

    assert ops.pin("file", parent).path == str(tmp_path / "file")
    assert ops.pin("missing", parent) is None
    assert ops.pin("link", parent) is None
    with pytest.raises(OSError):
        ops.stat_name("link", parent)
    with pytest.raises(OSError):
        ops.stat_name("missing", parent)
    (tmp_path / "file").chmod(0o444)
    assert stat.S_ISREG(ops.stat_name("file", parent).st_mode)
    ops.chmod_name("file", parent, 0o644)
    assert mode(tmp_path / "file") & stat.S_IWUSR
    with pytest.raises(NotADirectoryError):
        ops.open_dir("link", parent)
    with pytest.raises(NotADirectoryError):
        ops.open_dir("file", parent)
    ops.close(parent)


def test_the_path_ops_refuse_to_change_an_entry_that_is_not_what_was_looked_at(tmp_path):
    ops = fileops._PathTreeOps()
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "file").chmod(0o444)
    pinned = ops.pin("file", str(tmp_path))
    (tmp_path / "file").chmod(0o644)    # Windows will not delete a read-only file
    (tmp_path / "file").unlink()
    (tmp_path / "file").write_text("y", encoding="utf-8")    # a different file under the same name
    (tmp_path / "file").chmod(0o444)
    if os.path.samestat(pinned.st, (tmp_path / "file").lstat()):
        pytest.skip("this file system reused the file's identity")

    with pytest.raises(OSError):
        ops.chmod(pinned, 0o644)

    assert mode(tmp_path / "file") == 0o444


def test_a_name_that_is_not_there_is_not_a_reparse_point(tmp_path):
    assert not fileops._is_reparse_point(str(tmp_path / "missing"))


# --- The descriptor operations ---

@posix_only
def test_without_o_path_read_only_directories_are_still_granted(tmp_path, monkeypatch, fd_ops):
    monkeypatch.setattr(fileops, "_O_PATH", 0)   # the descriptor route macOS takes: no O_PATH
    tree = tmp_path / "tree"
    (tree / "read_only").mkdir(parents=True)
    (tree / "read_only" / "object").write_text("x", encoding="utf-8")
    (tree / "read_only" / "object").chmod(0o444)
    (tree / "read_only").chmod(0o500)

    fileops.remove_tree(tree)

    assert not tree.exists()


@posix_only
def test_a_fifo_without_a_writer_does_not_stall_the_walk(tmp_path, monkeypatch, fd_ops):
    monkeypatch.setattr(fileops, "_O_PATH", 0)   # without O_PATH a FIFO is opened for real, so it must not block
    tree = tmp_path / "tree"
    tree.mkdir()
    os.mkfifo(tree / "pipe")
    worker = threading.Thread(target=grant_tree_owner_access, args=(tree,), daemon=True)

    worker.start()
    worker.join(10)

    assert not worker.is_alive()


@posix_only
def test_without_o_path_a_directory_granted_by_name_keeps_links_out_of_it(tmp_path, monkeypatch, fd_ops):
    tree = tmp_path / "tree"
    (tree / "sealed").mkdir(parents=True)
    (tree / "sealed").chmod(0o000)
    real_chmod = os.chmod
    asked_to_follow = []

    def chmod_by_name(path, mode, *, dir_fd=None, follow_symlinks=True):
        if isinstance(path, str):   # by name; the descriptors of pinned entries go by number
            asked_to_follow.append(follow_symlinks)
        return real_chmod(path, mode, dir_fd=dir_fd)   # Linux can't leave links alone: this only records the request
    monkeypatch.setattr(fileops, "_O_PATH", 0)
    monkeypatch.setattr(fileops, "_CHMOD_NOFOLLOW", True)
    monkeypatch.setattr(os, "chmod", chmod_by_name)

    grant_tree_owner_access(tree)
    monkeypatch.undo()

    assert asked_to_follow == [False]
    assert mode(tree / "sealed") & stat.S_IRWXU == stat.S_IRWXU


@posix_only
def test_without_o_path_an_unreadable_file_is_left_alone(tmp_path, monkeypatch, fd_ops):
    monkeypatch.setattr(fileops, "_O_PATH", 0)
    monkeypatch.setattr(fileops, "_CHMOD_NOFOLLOW", False)   # where chmod can't avoid links, nothing is changed
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "secret").write_text("x", encoding="utf-8")
    (tree / "secret").chmod(0o000)

    grant_tree_owner_access(tree)

    assert mode(tree / "secret") == 0o000
    (tree / "secret").chmod(0o600)


@posix_only
def test_a_directory_whose_owner_access_cannot_be_granted_is_left_and_not_raised(tmp_path, monkeypatch, fd_ops):
    sealed = tmp_path / "tree" / "sealed"
    sealed.mkdir(parents=True)
    sealed.chmod(0o000)
    real_chmod = os.chmod

    def refuse_chmod_of_sealed(path, mode, **kwargs):   # by descriptor (/proc) or by name: whichever route is taken
        if str(path).startswith("/proc/") or str(path).endswith("sealed"):
            raise PermissionError(path)
        return real_chmod(path, mode, **kwargs)
    monkeypatch.setattr(os, "chmod", refuse_chmod_of_sealed)
    try:
        grant_tree_owner_access(tmp_path / "tree")
        assert mode(sealed) == 0o000
    finally:
        monkeypatch.undo()
        sealed.chmod(0o700)


@posix_only
def test_clone_removal_gives_up_quietly_when_the_tree_stays(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()

    def refuse(path):
        raise PermissionError(path)
    monkeypatch.setattr(clone, "remove_tree", refuse)

    clone._remove_tree(tree)   # returns after its retries; the tree is still there for the caller to report

    assert tree.exists()
