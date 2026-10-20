"""Trees the server removes go even when a step left them without owner access (#754).

The cleanup used to chmod every entry to 0o200 (`stat.S_IWRITE` on POSIX replaces the mode), so the next listing of
that directory failed, and the tree stayed under pytest's basetemp for pytest's own cleanup to trip over.
"""

import stat
import sys

import pytest

from harness import app_stores, clone, maintenance
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
