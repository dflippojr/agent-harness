"""TEMPORARY diagnostic for #878, reverted before the PR is ready: it always fails on Windows so CI prints its timings."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
$ErrorView = 'NormalView'
. (Join-Path $env:DIAG_ROOT 'ops/review/run-review.ps1')
$dir = Join-Path $env:DIAG_TMP 'fake'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$gh = Join-Path $dir 'gh.cmd'
@(
    '@echo off'
    'echo cmd-start %time%>> "%~dp0stages.txt"'
    'echo %*> "%~dp0args.txt"'
    'powershell -NoProfile -NonInteractive -Command "[Console]::InputEncoding = New-Object Text.UTF8Encoding($false); [IO.File]::WriteAllText(''%~dp0stdin.txt'', [Console]::In.ReadToEnd())"'
    'echo stdin-captured %time%>> "%~dp0stages.txt"'
    'if defined FAKE_GH_SLEEP powershell -NoProfile -NonInteractive -Command "Start-Sleep %FAKE_GH_SLEEP%"'
    'if defined FAKE_GH_HTML echo %FAKE_GH_HTML%'
    'exit /b %FAKE_GH_EXIT%'
) | Set-Content -LiteralPath $gh -Encoding ascii
$stages = Join-Path $dir 'stages.txt'

function Measure-Exe([string]$File, [string]$Arguments) {
    $w = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -NoNewWindow -PassThru -Wait
    [Math]::Round($w.Elapsed.TotalSeconds, 2)
}
function Get-Load { (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average }
function Invoke-Case([string]$Label, $Sleep, $Html, [string]$Exit, [int]$Budget) {
    $env:FAKE_GH_SLEEP = $Sleep; $env:FAKE_GH_HTML = $Html; $env:FAKE_GH_EXIT = $Exit
    Remove-Item -LiteralPath $stages -ErrorAction SilentlyContinue
    $w = [Diagnostics.Stopwatch]::StartNew(); $name = 'none'
    try { Invoke-ReviewMarkdownRender -Text 'body' -FilePath $gh -TimeoutSeconds $Budget | Out-Null }
    catch { $name = $_.Exception.GetType().Name }
    $elapsed = [Math]::Round($w.Elapsed.TotalSeconds, 2)
    $trace = if (Test-Path -LiteralPath $stages) { (Get-Content -LiteralPath $stages) -join ' | ' } else { 'no stages' }
    $orphans = @(Get-Process powershell -ErrorAction SilentlyContinue).Count
    "DIAG $Label budget=$Budget elapsed=$elapsed exception=$name load=$(Get-Load) powershell-procs=$orphans :: $trace"
}

"DIAG logical-cpus=$([Environment]::ProcessorCount) ps-version=$($PSVersionTable.PSVersion)"
foreach ($round in 1..3) {
    "DIAG raw round=$round cmd=$(Measure-Exe 'cmd.exe' '/c exit') powershell=$(Measure-Exe 'powershell.exe' '-NoProfile -NonInteractive -Command exit') pwsh=$(Measure-Exe 'pwsh.exe' '-NoProfile -NonInteractive -Command exit') load=$(Get-Load)"
}
# A: the no-sleep cases alone, with room to show their true length; no killed process before them.
foreach ($round in 1..6) {
    Invoke-Case "A$round-fails" $null 'x' '1' 60
    Invoke-Case "A$round-empty" $null $null '0' 60
}
# B: the order the Pester table runs: the timeout case first (it kills cmd and orphans Start-Sleep), then the two cases.
foreach ($round in 1..6) {
    Invoke-Case "B$round-timeout" '20' 'x' '0' 3
    Invoke-Case "B$round-fails" $null 'x' '1' 60
    Invoke-Case "B$round-empty" $null $null '0' 60
}
Import-Module Pester -ErrorAction Stop
foreach ($round in 1..3) {
    "DIAG pester round=$round"
    Invoke-Pester -Path (Join-Path $env:DIAG_ROOT 'tests/review-output.Tests.ps1') -FullName '*renderer call*' -Output Detailed | Out-Null
}
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows timing only")
def test_diag878_windows_timings(tmp_path):
    wrapper = tmp_path / "diag.ps1"
    wrapper.write_text(SCRIPT, encoding="utf-8")
    env = dict(os.environ, DIAG_ROOT=str(ROOT), DIAG_TMP=str(tmp_path), NO_COLOR="1")
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env=env,
    )
    pytest.fail("DIAG-OUTPUT\n" + result.stdout + "\nSTDERR\n" + result.stderr)
