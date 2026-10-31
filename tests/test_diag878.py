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
$stamp = '(Get-Date).ToString(''HH:mm:ss.ff'')'
@(
    '@echo off'
    'echo cmd-start %time%>> "%~dp0stages.txt"'
    'echo %*> "%~dp0args.txt"'
    ('powershell -NoProfile -NonInteractive -Command "$begin = ' + $stamp + '; $created = [Diagnostics.Process]::GetCurrentProcess().StartTime.ToString(''HH:mm:ss.ff''); [Console]::InputEncoding = New-Object Text.UTF8Encoding($false); $in = [Console]::In.ReadToEnd(); $read = ' + $stamp + '; [IO.File]::WriteAllText(''%~dp0stdin.txt'', $in); $wrote = ' + $stamp + '; Add-Content -LiteralPath ''%~dp0stages.txt'' (''ps-created '' + $created + '' cmd-begin '' + $begin + '' stdin-read '' + $read + '' file-written '' + $wrote)"')
    'echo ps-exited %time%>> "%~dp0stages.txt"'
    'if "%FAKE_GH_MODE%"=="startsleep" powershell -NoProfile -NonInteractive -Command "Start-Sleep 8"'
    'if "%FAKE_GH_MODE%"=="threadsleep" powershell -NoProfile -NonInteractive -Command "[Threading.Thread]::Sleep(8000)"'
    'if "%FAKE_GH_MODE%"=="ping" ping -n 9 127.0.0.1 >nul'
    'if defined FAKE_GH_HTML echo %FAKE_GH_HTML%'
    'exit /b %FAKE_GH_EXIT%'
) | Set-Content -LiteralPath $gh -Encoding ascii
$stages = Join-Path $dir 'stages.txt'

function Invoke-Case([string]$Label, [string]$Mode, $Html, [string]$Exit, [int]$Budget) {
    $env:FAKE_GH_MODE = $Mode; $env:FAKE_GH_HTML = $Html; $env:FAKE_GH_EXIT = $Exit
    Remove-Item -LiteralPath $stages -ErrorAction SilentlyContinue
    $w = [Diagnostics.Stopwatch]::StartNew(); $name = 'none'
    try { Invoke-ReviewMarkdownRender -Text 'body' -FilePath $gh -TimeoutSeconds $Budget | Out-Null }
    catch { $name = $_.Exception.GetType().Name }
    $elapsed = [Math]::Round($w.Elapsed.TotalSeconds, 2)
    $trace = if (Test-Path -LiteralPath $stages) { (Get-Content -LiteralPath $stages) -join ' | ' } else { 'no stages' }
    $procs = @(Get-Process powershell -ErrorAction SilentlyContinue).Count
    "DIAG $Label budget=$Budget elapsed=$elapsed exception=$name powershell-procs=$procs :: $trace"
}
function Wait-Quiet {
    $w = [Diagnostics.Stopwatch]::StartNew()
    while ($w.Elapsed.TotalSeconds -lt 12 -and (@(Get-Process powershell, ping -ErrorAction SilentlyContinue).Count -gt 0)) { Start-Sleep -Milliseconds 200 }
    [Threading.Thread]::Sleep(300)
}
function Invoke-Variant([string]$Variant, [string]$Round) {
    Wait-Quiet
    $load = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
    "DIAG $Round-$Variant load=$load"
    switch ($Variant) {
        # Nothing before the two cases.
        'none' { }
        # A PowerShell that runs Start-Sleep to its end, with no kill and no orphan.
        'done' { powershell -NoProfile -NonInteractive -Command 'Start-Sleep 3' }
        # The kill, leaving an orphan that is not PowerShell.
        'killping' { Invoke-Case "$Round-killping-timeout" 'ping' 'x' '0' 3 }
        # The kill, leaving a PowerShell orphan that never loads a module for Start-Sleep.
        'killthread' { Invoke-Case "$Round-killthread-timeout" 'threadsleep' 'x' '0' 3 }
        # The kill as the Pester table does it: a PowerShell orphan in Start-Sleep.
        'killstart' { Invoke-Case "$Round-killstart-timeout" 'startsleep' 'x' '0' 3 }
    }
    Invoke-Case "$Round-$Variant-fails" '' 'x' '1' 60
    Invoke-Case "$Round-$Variant-empty" '' $null '0' 60
}

$variants = @('none', 'done', 'killping', 'killthread', 'killstart')
foreach ($round in 0..4) {
    # Rotated, so no variant always runs first or after the same neighbour.
    foreach ($i in 0..4) { Invoke-Variant $variants[($i + $round) % 5] "R$round" }
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
