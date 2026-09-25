"""Issue #221: fileops.write_text_within confines a write to its directory and replaces the file atomically."""

from __future__ import annotations

import os

import pytest

from harness.fileops import ToolError, write_text_within


def test_writes_and_replaces_inside_the_root(tmp_path):
    written = write_text_within(tmp_path, tmp_path / "sub" / "a.md", "one\n")
    assert written == (tmp_path / "sub" / "a.md").resolve()
    assert written.read_text(encoding="utf-8") == "one\n"
    write_text_within(tmp_path, tmp_path / "sub" / "a.md", "two\n")
    assert written.read_text(encoding="utf-8") == "two\n"
    assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["a.md"]  # no temp file left behind


def test_refuses_a_target_outside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ToolError, match="outside"):
        write_text_within(root, root / ".." / "escaped.md", "x")
    with pytest.raises(ToolError, match="outside"):
        write_text_within(root, tmp_path / "elsewhere.md", "x")
    with pytest.raises(ToolError, match="outside"):
        write_text_within(root, root, "x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["root"]


def test_refuses_a_symlink_that_leads_out_of_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    try:
        (root / "link.md").symlink_to(outside)
    except OSError:
        pytest.skip("this account can't create symlinks")
    with pytest.raises(ToolError, match="outside"):
        write_text_within(root, root / "link.md", "overwritten\n")
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_breaks_a_hard_link_instead_of_writing_through_it(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.link(outside, root / "linked.md")
    write_text_within(root, root / "linked.md", "new\n")
    assert (root / "linked.md").read_text(encoding="utf-8") == "new\n"
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_a_failed_write_keeps_the_old_file(tmp_path):
    target = tmp_path / "profile.md"
    target.write_text("# Profile\n", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        write_text_within(tmp_path, target, "half \ud800 written")
    assert target.read_text(encoding="utf-8") == "# Profile\n"
    assert [p.name for p in tmp_path.iterdir()] == ["profile.md"]
