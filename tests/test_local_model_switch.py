"""The local-model switch (#413): run-qwen.ps1 serves the harness's default_model, so one config line switches."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "ops" / "llama-server" / "run-qwen.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

# Loads $localModels, $commonArgs and the two functions from run-qwen.ps1 without running its supervisor loop, points
# the model files at $Files and the config at $ConfigDir, then prints the args it would start llama-server with.
PROBE = r"""
param([string]$ConfigDir, [string]$Files, [string]$Model = '')
$ast = [Management.Automation.Language.Parser]::ParseFile($env:SUPERVISOR, [ref]$null, [ref]$null)
foreach ($node in $ast.EndBlock.Statements) {
    $text = $node.Extent.Text
    if ($text -match '^\$(localModels|fallbackModel|commonArgs) =' -or $text -match '^function Get-') {
        Invoke-Expression $text
    }
}
$configDir = $ConfigDir
function Log($msg) { Write-Output "LOG $msg" }
foreach ($spec in $localModels.Values) {
    $spec.path = Join-Path $Files ([IO.Path]::GetFileName($spec.path))
}
$serverArgs = Get-ServerArgs
Write-Output "ARGS $($serverArgs -join ' ')"
"""


def _config():
    return yaml.safe_load((ROOT / "config" / "harness.yaml").read_text(encoding="utf-8"))


def test_production_default_is_unchanged_and_both_models_are_configured():
    cfg = _config()
    assert cfg["default_model"] == "qwen3.6-35b-a3b"  # the owner switches after merge, in harness.local.yaml
    old, new = cfg["models"]["qwen3.6-35b-a3b"], cfg["models"]["qwen3.8-35b-a3b-distill"]
    assert new["base_url"] == old["base_url"] == "http://127.0.0.1:8090"
    assert new["sampling"] == old["sampling"] == {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
    assert new["context_tokens"] == old["context_tokens"]


def test_supervisor_lists_every_local_model_pinned_like_the_study():
    text = SUPERVISOR.read_text(encoding="utf-8")
    for name in _config()["models"]:
        assert f"'{name}'" in text
    assert "C:/AI/models/Qwen3.8-35B-A3B-Q4_K_M.gguf" in text
    assert "b1f9d1dcc3de8aa867669b0ab919384aeeb9b8d5" in text
    assert "196103269085bc54c9b8f49ed21e9f53e1b56b465e8b796c6d8e31e06f63cfa5" in text


def _probe(tmp_path: Path, base: str, local: str | None = None, files=("Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
           "Qwen3.8-35B-A3B-Q4_K_M.gguf"), model: str = "") -> str:
    config_dir, models = tmp_path / "config", tmp_path / "models"
    config_dir.mkdir(parents=True)
    models.mkdir()
    (config_dir / "harness.yaml").write_text(base, encoding="utf-8")
    if local is not None:
        (config_dir / "harness.local.yaml").write_text(local, encoding="utf-8")
    for name in files:
        (models / name).write_bytes(b"")
    assert POWERSHELL is not None
    probe = tmp_path / "probe.ps1"
    probe.write_text(PROBE, encoding="utf-8")
    args = ["-File", str(probe), "-ConfigDir", str(config_dir), "-Files", str(models)]
    if model:
        args += ["-Model", model]
    result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             *args], capture_output=True, text=True, timeout=120, check=False,
                            env={**os.environ, "SUPERVISOR": str(SUPERVISOR)})
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
BASE = "default_model: qwen3.6-35b-a3b\nmodels:\n  qwen3.6-35b-a3b: {}\n"


@needs_powershell
def test_serves_the_base_default(tmp_path):
    out = _probe(tmp_path, BASE)
    assert "-m " in out and "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --alias qwen3.6-35b-a3b --host 127.0.0.1" in out
    assert out.count("--load-mode none") == 1
    assert "LOG" not in out


@needs_powershell
def test_one_local_line_switches_and_removing_it_rolls_back(tmp_path):
    out = _probe(tmp_path, BASE, local="# owner\ndefault_model: \"qwen3.8-35b-a3b-distill\"  # #413\n")
    assert "Qwen3.8-35B-A3B-Q4_K_M.gguf --alias qwen3.8-35b-a3b-distill --host 127.0.0.1" in out
    assert out.count("--load-mode none") == 1


@needs_powershell
def test_unknown_model_or_missing_file_falls_back_to_qwen36(tmp_path):
    out = _probe(tmp_path / "a", BASE, local="default_model: some-hosted-model\n")
    assert "is not in run-qwen.ps1's list" in out and "--alias qwen3.6-35b-a3b" in out
    out = _probe(tmp_path / "b", BASE, local="default_model: qwen3.8-35b-a3b-distill\n",
                 files=("Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",))
    assert "is missing" in out and "--alias qwen3.6-35b-a3b" in out


@needs_powershell
def test_model_parameter_pins_a_model(tmp_path):
    out = _probe(tmp_path, BASE, model="qwen3.8-35b-a3b-distill")
    assert "--alias qwen3.8-35b-a3b-distill" in out
