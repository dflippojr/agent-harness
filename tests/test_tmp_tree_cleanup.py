"""Trees the server removes go even when a step left them without owner access (#754).

The cleanup used to chmod every entry to 0o200 (`stat.S_IWRITE` on POSIX replaces the mode), so the next listing of
that directory failed, and the tree stayed under pytest's basetemp for pytest's own cleanup to trip over.
"""

import os
import stat
import sys
import threading

import pytest

from harness import app_stores, clone, fileops, maintenance
from harness.fileops import grant_tree_owner_access
from macrunner import harness_runner

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")

REMOVERS = [
    pytest.param(clone._remove_tree, id="clone"),
    pytest.param(maintenance.remove_tree, id="maintenance"),
    pytest.param(app_stores.remove_tree, id="app_stores"),
    pytest.param(harness_runner.remove_tree, id="macrunner"),
]


@posix_only
@pytest.mark.parametrize("remove", REMOVERS)
def test_a_tree_with_no_owner_access_is_removed(tmp_path, remove):
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


@posix_only
@pytest.mark.parametrize("remove", REMOVERS)
def test_removing_a_link_leaves_what_it_points_to_alone(tmp_path, remove):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x", encoding="utf-8")
    outside.chmod(0o755)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "elsewhere").symlink_to(outside, target_is_directory=True)
    (tree / "locked").mkdir()
    (tree / "locked").chmod(0o500)

    remove(tree)

    assert not tree.exists()
    assert (outside / "keep").read_text(encoding="utf-8") == "x"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


@posix_only
def test_granting_access_to_a_link_root_leaves_its_target_alone(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x", encoding="utf-8")
    (outside / "keep").chmod(0o444)
    outside.chmod(0o500)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)

    grant_tree_owner_access(link)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    assert stat.S_IMODE((outside / "keep").stat().st_mode) == 0o444


@posix_only
def test_a_name_swapped_for_a_link_after_the_walk_listed_it_is_not_followed(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    real_pin = fileops._pin
    swapped = []

    def swap_then_pin(name, dir_fd):
        if name == "sub" and not swapped:   # another process replaces the directory the walk has just listed
            swapped.append(name)
            (tree / "sub").rmdir()
            (tree / "sub").symlink_to(outside, target_is_directory=True)
        return real_pin(name, dir_fd)
    monkeypatch.setattr(fileops, "_pin", swap_then_pin)

    grant_tree_owner_access(tree)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o500


@posix_only
def test_without_o_path_read_only_directories_are_still_granted(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "_O_PATH", 0)   # the descriptor route macOS takes: no O_PATH
    tree = tmp_path / "tree"
    (tree / "read_only").mkdir(parents=True)
    (tree / "read_only" / "object").write_text("x", encoding="utf-8")
    (tree / "read_only" / "object").chmod(0o444)
    (tree / "read_only").chmod(0o500)

    fileops.remove_tree(tree)

    assert not tree.exists()


@posix_only
def test_a_fifo_without_a_writer_does_not_stall_the_walk(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "_O_PATH", 0)   # without O_PATH a FIFO is opened for real, so it must not block
    tree = tmp_path / "tree"
    tree.mkdir()
    os.mkfifo(tree / "pipe")
    worker = threading.Thread(target=grant_tree_owner_access, args=(tree,), daemon=True)

    worker.start()
    worker.join(10)

    assert not worker.is_alive()


@posix_only
def test_without_o_path_a_directory_granted_by_name_keeps_links_out_of_it(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    (tree / "sealed").mkdir(parents=True)
    (tree / "sealed").chmod(0o000)
    real_chmod = os.chmod
    asked_to_follow = []

    def chmod_by_name(path, mode, *, dir_fd=None, follow_symlinks=True):
        asked_to_follow.append(follow_symlinks)
        return real_chmod(path, mode, dir_fd=dir_fd)   # Linux can't leave links alone: this only records the request
    monkeypatch.setattr(fileops, "_O_PATH", 0)
    monkeypatch.setattr(fileops, "_CHMOD_NOFOLLOW", True)
    monkeypatch.setattr(os, "chmod", chmod_by_name)

    grant_tree_owner_access(tree)
    monkeypatch.undo()

    assert asked_to_follow == [False]
    assert stat.S_IMODE((tree / "sealed").stat().st_mode) & stat.S_IRWXU == stat.S_IRWXU


@posix_only
def test_without_o_path_an_unreadable_file_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "_O_PATH", 0)
    monkeypatch.setattr(fileops, "_CHMOD_NOFOLLOW", False)   # where chmod can't avoid links, nothing is changed
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "secret").write_text("x", encoding="utf-8")
    (tree / "secret").chmod(0o000)

    grant_tree_owner_access(tree)

    assert stat.S_IMODE((tree / "secret").stat().st_mode) == 0o000
    (tree / "secret").chmod(0o600)


@posix_only
def test_an_entry_that_vanished_before_it_was_reached_is_skipped(tmp_path):
    parent = os.open(tmp_path, os.O_RDONLY)
    try:
        fileops._grant_entry("missing", parent)
    finally:
        os.close(parent)


@posix_only
def test_a_directory_whose_owner_access_cannot_be_granted_is_left_and_not_raised(tmp_path, monkeypatch):
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
        assert stat.S_IMODE(sealed.stat().st_mode) == 0o000
    finally:
        monkeypatch.undo()
        sealed.chmod(0o700)


def test_a_tree_whose_parent_is_missing_is_left_alone(tmp_path):
    grant_tree_owner_access(tmp_path / "missing" / "tree")


@posix_only
def test_the_windows_walk_grants_owner_access_and_skips_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x", encoding="utf-8")
    (outside / "keep").chmod(0o444)
    outside.chmod(0o500)
    tree = tmp_path / "tree"
    (tree / "read_only").mkdir(parents=True)
    (tree / "read_only" / "object").write_text("x", encoding="utf-8")
    (tree / "read_only" / "object").chmod(0o444)
    (tree / "read_only").chmod(0o500)
    (tree / "elsewhere").symlink_to(outside, target_is_directory=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    fileops._grant_by_path(tree)

    assert stat.S_IMODE((tree / "read_only").stat().st_mode) & stat.S_IRWXU == stat.S_IRWXU
    assert stat.S_IMODE((tree / "read_only" / "object").stat().st_mode) & stat.S_IWUSR
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    assert stat.S_IMODE((outside / "keep").stat().st_mode) == 0o444
    assert not fileops._is_reparse_point(str(tmp_path / "missing"))
    fileops._add_owner_bits(str(tmp_path / "missing"))   # a name that vanished is skipped, not an error

    fileops._grant_by_path(tmp_path / "link")            # a link given as the root is not walked either


@posix_only
def test_where_descriptors_are_unavailable_the_path_walk_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "_DIR_FD", False)
    tree = tmp_path / "tree"
    (tree / "read_only").mkdir(parents=True)
    (tree / "read_only").chmod(0o500)

    grant_tree_owner_access(tree)

    assert stat.S_IMODE((tree / "read_only").stat().st_mode) & stat.S_IRWXU == stat.S_IRWXU


@posix_only
def test_clone_removal_gives_up_quietly_when_the_tree_stays(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()

    def refuse(path):
        raise PermissionError(path)
    monkeypatch.setattr(clone, "remove_tree", refuse)

    clone._remove_tree(tree)   # returns after its retries; the tree is still there for the caller to report

    assert tree.exists()


@posix_only
def test_removing_a_tree_that_is_already_gone_is_a_no_op(tmp_path):
    fileops.remove_tree(tmp_path / "missing")
