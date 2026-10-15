# Supervisor for the harness daemon. Started hidden at logon by the "AgentHarness-Daemon" scheduled task
# (see install-task.ps1). Restarts the daemon if it exits. Sessions that were running resume on restart.
# Sets HARNESS_SUPERVISED=1 so the daemon can advertise a supervised restart, and HARNESS_STABLES_DIR from the
# deployment's .stables-path.
$ErrorActionPreference = 'Stop'

$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$venv = Join-Path $root '.venv'
. (Join-Path $PSScriptRoot 'deploy-commit.ps1')
$buildCommit = ''
$venvPointer = Join-Path $root '.venv-path'
if (Test-Path -LiteralPath $venvPointer) {
    $pointerValue = Get-Content -Raw -LiteralPath $venvPointer
    if ($null -eq $pointerValue) { throw "empty deployment virtual environment pointer: $venvPointer" }
    $venv = ([string]$pointerValue).Trim()
    if (-not [System.IO.Path]::IsPathRooted($venv)) { throw "invalid deployment virtual environment pointer: $venvPointer" }
    $buildCommit = Get-DeployedCommit $venv
}
# The deployment's pinned Stables copy (deploy-ci.ps1), served through HARNESS_STABLES_DIR. Without the pointer the
# daemon looks for a Stables clone in the checkout and otherwise runs the API alone.
$stablesSetting = ''
$stablesPointer = Join-Path $root '.stables-path'
if (Test-Path -LiteralPath $stablesPointer) {
    $stables = ([string](Get-Content -Raw -LiteralPath $stablesPointer)).Trim()
    if (-not [System.IO.Path]::IsPathRooted($stables)) { throw "invalid Stables pointer: $stablesPointer" }
    # Quoted, so cmd reads characters such as & in the path as part of the value.
    $stablesSetting = "set `"HARNESS_STABLES_DIR=$stables`"&& "
}
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "harness Python is missing: $python" }
$logDir = 'D:\Agents\harness\logs'
$daemonLog = Join-Path $logDir 'daemon.log'
$supervisorLog = Join-Path $logDir 'supervisor.log'

New-Item -ItemType Directory -Force $logDir | Out-Null

# One supervisor per logon session, even if the task is started twice.
$mutex = New-Object System.Threading.Mutex($false, 'Local\AgentHarnessDaemon')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

while ($true) {
    if ((Test-Path $daemonLog) -and (Get-Item $daemonLog).Length -gt 20MB) { Move-Item $daemonLog "$daemonLog.prev" -Force }
    Log 'starting daemon'
    # cmd handles the append redirect so both streams land in one log without PowerShell wrapping stderr.
    $proc = Start-Process cmd.exe -ArgumentList "/c `"set HARNESS_SUPERVISED=1&& set HARNESS_BUILD_COMMIT=$buildCommit&& $stablesSetting`"$python`" -u -m harness >> `"$daemonLog`" 2>&1`"" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    Log "daemon exited with code $($proc.ExitCode); restarting in 10s"
    Start-Sleep -Seconds 10
}
