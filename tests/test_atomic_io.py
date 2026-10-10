import os
import json
import stat
import subprocess

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


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited ACLs")
def test_owner_only_directory_protects_future_children(tmp_path):
    from harness.atomic_io import owner_only_acl
    directory = tmp_path / "hub-state"
    directory.mkdir()
    owner_only_acl(directory, inherit=True)
    child = directory / "claim.json"
    child.write_text("{}")
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    script = (
        f"$parentAcl = Get-Acl -LiteralPath {quote(directory)}; "
        f"$childAcl = Get-Acl -LiteralPath {quote(child)}; "
        "$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "[ordered]@{ protected=$parentAcl.AreAccessRulesProtected; current=$currentSid; "
        "childSids=@($childAcl.Access | ForEach-Object { "
        "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }); "
        "inherited=@($childAcl.Access | Where-Object IsInherited).Count } | ConvertTo-Json"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, check=True, timeout=30)
    acl = json.loads(result.stdout)
    assert acl["protected"] and acl["inherited"] == 1
    assert acl["childSids"] == [acl["current"]]
