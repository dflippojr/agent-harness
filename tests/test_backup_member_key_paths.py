"""Issue #728: the member-key directory and private writes refuse paths that leave where they belong.

Synthetic paths and bytes only; nothing outside tmp_path is created."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import config
from harness.config import BackupConfig
from harness_modules.backup import member_key

UNC_AND_DEVICE = ["\\\\synthetic-server\\share\\keys", "//synthetic-server/share/keys",
                  "\\\\?\\C:\\synthetic\\keys", "\\\\.\\C:\\synthetic\\keys"]


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a Windows junction where symlinks need privileges the test may not have."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("can't create a directory link here")


def _cfg(root: Path, member_key_dir: str = "") -> SimpleNamespace:
    return SimpleNamespace(backup=BackupConfig(dir=str(root), member_key_dir=member_key_dir))


def _refused(root: Path, value: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        config.check_member_key_dir(value)
    with pytest.raises(ValueError, match=match):
        member_key.key_dir(_cfg(root, value), root)


@pytest.mark.parametrize("value", [os.path.join("..", ".."), "..\\..\\synthetic", "../../synthetic", "member-keys"])
def test_relative_member_key_dir_is_refused(tmp_path, value):
    _refused(tmp_path / "backups", value, "absolute|'\\.\\.'")


def test_dot_dot_segments_are_refused_even_when_absolute(tmp_path):
    _refused(tmp_path / "backups", str(tmp_path / "backups" / ".." / ".." / "elsewhere"), "'\\.\\.'")


@pytest.mark.parametrize("value", UNC_AND_DEVICE)
def test_unc_and_device_paths_are_refused(tmp_path, value):
    _refused(tmp_path / "backups", value, "UNC or device|absolute")


def test_configured_directory_through_a_link_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "keys-link"
    _link_dir(link, outside)
    _refused(tmp_path / "backups", str(link), "symlink")
    _refused(tmp_path / "backups", str(link / "nested"), "symlink")


def test_default_directory_link_out_of_backup_dir_is_refused(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_dir(root / "member-keys", outside)
    with pytest.raises(ValueError, match="inside backup.dir"):
        member_key.key_dir(_cfg(root), root)


def test_default_and_configured_directories_resolve_once(tmp_path):
    root = tmp_path / "backups"
    assert member_key.key_dir(_cfg(root), root) == (root / "member-keys").resolve()
    separate = tmp_path / "separate" / "keys"
    assert member_key.key_dir(_cfg(root, str(separate)), root) == separate.resolve()
    assert config.check_member_key_dir(str(separate)) == separate.resolve()


def test_config_load_refuses_a_bad_member_key_dir(tmp_path):
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parent.parent / "config", cfg_dir)
    good = (tmp_path / "keys").as_posix()
    with (cfg_dir / "harness.yaml").open("a", encoding="utf-8") as f:
        f.write(f"\nbackup:\n  member_key_dir: '{good}'\n")
    assert config.load(cfg_dir, data_dir=tmp_path / "data").backup.member_key_dir == good
    text = (cfg_dir / "harness.yaml").read_text(encoding="utf-8")
    (cfg_dir / "harness.yaml").write_text(text.replace(good, "//synthetic-server/share/keys"), encoding="utf-8")
    with pytest.raises(ValueError, match="UNC or device"):
        config.load(cfg_dir, data_dir=tmp_path / "data")


@pytest.mark.parametrize("name", ["../escape.key", "nested/escape.key", "..", ""]
                         + (["..\\escape.key", "C:escape.key"] if os.name == "nt" else []))
def test_private_write_refuses_names_outside_the_directory(tmp_path, name):
    directory = tmp_path / "keys"
    with pytest.raises(ValueError, match="refusing to write"):
        member_key.write_private(directory, name, b"synthetic")
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # refused before mkdir or mkstemp


def test_private_write_refuses_an_absolute_path_elsewhere(tmp_path):
    elsewhere = tmp_path / "elsewhere" / "escape.key"
    with pytest.raises(ValueError, match="refusing to write"):
        member_key.write_private(tmp_path / "keys", str(elsewhere), b"synthetic")
    assert not list(tmp_path.iterdir())


def test_private_write_refuses_a_target_linked_outside(tmp_path):
    directory = tmp_path / "keys"
    directory.mkdir()
    outside = tmp_path / "outside.key"
    outside.write_bytes(b"untouched")
    try:
        os.symlink(outside, directory / "escape.key")
    except (OSError, NotImplementedError):
        pytest.skip("can't create a file symlink here")
    with pytest.raises(ValueError, match="refusing to write"):
        member_key.write_private(directory, "escape.key", b"synthetic")
    assert outside.read_bytes() == b"untouched"
    assert sorted(p.name for p in directory.iterdir()) == ["escape.key"]


def test_private_write_through_a_linked_directory_stays_inside_it(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _link_dir(link, real)
    written = member_key.write_private(link, "synthetic.key", b"synthetic")
    assert written == (real / "synthetic.key").resolve()
    assert (real / "synthetic.key").read_bytes() == b"synthetic"
