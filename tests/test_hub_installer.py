"""Hub is selectable through the installer as well as the config generator."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from harness import config, setup_config


def test_service_setup_can_enable_hub_without_local_model(tmp_path):
    assert setup_config.main(["--config-dir", str(tmp_path / "cfg"), "--data-dir", str(tmp_path / "data"),
                              "--pause-flag", str(tmp_path / "paused"),
                              "--profile", "service", "--enable-module", "hub"]) == 0
    cfg = config.load(tmp_path / "cfg")
    assert cfg.hub.enabled and cfg.installed.hub and cfg.modules.hub
    assert cfg.models == {} and not cfg.modules.local_model


def test_windows_installer_binds_hub_and_allowlist_matches_core(tmp_path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows installer parameter binding requires PowerShell")
    probe = tmp_path / "bind-installer.ps1"
    probe.write_text(r'''
param([string]$Installer)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors) { throw 'Installer parse failed' }
$parameter = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'EnableModules' }
$attribute = $parameter.Attributes | Where-Object { $_.TypeName.FullName -eq 'ValidateSet' }
# Compile and bind only the parameter block. No installer actions are executed.
$binding = [scriptblock]::Create($ast.ParamBlock.Extent.Text + [Environment]::NewLine + '$EnableModules')
$selected = @(& $binding -Profile Service -EnableModules hub -InstallDir $PSScriptRoot)
[pscustomobject]@{
    selected = $selected
    allowed = @($attribute.PositionalArguments | ForEach-Object { $_.SafeGetValue() })
} | ConvertTo-Json -Compress
''', encoding="utf-8")
    installer = Path(__file__).resolve().parents[1] / "install" / "install.ps1"
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(probe),
                             "-Installer", str(installer)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["selected"] == ["hub"]
    assert set(data["allowed"]) == set(config.MODULE_NAMES)
