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
$redirect = if ($env:DIAG_FAKE -eq 'fixed') { ' <nul >nul 2>nul' } else { '' }
@(
    '@echo off'
    'echo %*> "%~dp0args.txt"'
    'powershell -NoProfile -NonInteractive -Command "[Console]::InputEncoding = New-Object Text.UTF8Encoding($false); [IO.File]::WriteAllText(''%~dp0stdin.txt'', [Console]::In.ReadToEnd())"'
    ('if defined FAKE_GH_SLEEP powershell -NoProfile -NonInteractive -Command "Start-Sleep %FAKE_GH_SLEEP%"' + $redirect)
    'if defined FAKE_GH_HTML echo %FAKE_GH_HTML%'
    'exit /b %FAKE_GH_EXIT%'
) | Set-Content -LiteralPath $gh -Encoding ascii
function Get-Pool { "pool-threads=$([Threading.ThreadPool]::ThreadCount) pending=$([Threading.ThreadPool]::PendingWorkItemCount) completed=$([Threading.ThreadPool]::CompletedWorkItemCount)" }
function Invoke-Case([string]$Label, $Sleep, $Html, [string]$Exit, [int]$Budget) {
    $env:FAKE_GH_SLEEP = $Sleep; $env:FAKE_GH_HTML = $Html; $env:FAKE_GH_EXIT = $Exit
    $w = [Diagnostics.Stopwatch]::StartNew(); $name = 'none'
    try { Invoke-ReviewMarkdownRender -Text 'body' -FilePath $gh -TimeoutSeconds $Budget | Out-Null }
    catch { $name = $_.Exception.GetType().Name; $pool = Get-Pool }
    "DIAG $env:DIAG_LABEL $Label budget=$Budget elapsed=$([Math]::Round($w.Elapsed.TotalSeconds, 2)) exception=$name $pool"
}
$workers = 0; $io = 0; [Threading.ThreadPool]::GetMinThreads([ref]$workers, [ref]$io)
"DIAG $env:DIAG_LABEL cpus=$([Environment]::ProcessorCount) min-workers=$workers $(Get-Pool)"
if ($env:DIAG_MAX) { "DIAG $env:DIAG_LABEL set-max=$([Threading.ThreadPool]::SetMaxThreads([int]$env:DIAG_MAX, 1000))" }
foreach ($i in 1..[int]$env:DIAG_KILLS) { Invoke-Case "timeout$i" '20' 'x' '0' 3 }
foreach ($i in 1..3) {
    Invoke-Case "fails$i" $null 'x' '1' 3
    Invoke-Case "empty$i" $null $null '0' 3
}
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows timing only")
def test_diag878_windows_timings(tmp_path):
    wrapper = tmp_path / "diag.ps1"
    wrapper.write_text(SCRIPT, encoding="utf-8")
    report = []
    # Label, fake, kills before the cases, and the worker cap (the CPU count, so the pool cannot grow past blocked reads).
    for label, fake, kills, cap in (
        ("held-capped", "held", "2", "cpus"),
        ("fixed-capped", "fixed", "2", "cpus"),
        ("held-default", "held", "1", ""),
        ("fixed-default", "fixed", "1", ""),
    ):
        env = dict(os.environ, DIAG_ROOT=str(ROOT), DIAG_TMP=str(tmp_path / label), DIAG_LABEL=label, DIAG_FAKE=fake, DIAG_KILLS=kills, NO_COLOR="1")
        env["DIAG_MAX"] = str(os.cpu_count()) if cap else ""
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
            env=env,
        )
        report.append(result.stdout + "\nSTDERR\n" + result.stderr)
    pytest.fail("DIAG-OUTPUT\n" + "\n".join(report))
