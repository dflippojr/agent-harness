import os
import stat

import pytest

from harness.atomic_io import write_atomic


def test_replaces_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_private_file_is_owner_only(tmp_path):
    target = tmp_path / "secret.json"
    write_atomic(target, "{}", private=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failed_replace_removes_temp_and_keeps_old(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("no")

    monkeypatch.setattr("harness.atomic_io.os.replace", boom)
    with pytest.raises(OSError):
        write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
