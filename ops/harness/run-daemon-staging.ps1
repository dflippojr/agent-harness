# Supervisor for the staging harness daemon (issue #129), started hidden at logon by the
# "AgentHarness-Daemon-Staging" scheduled task (see install-task-staging.ps1). Restarts the daemon if it exits.
# Everything it touches is staging: the D:\Projects\agent-harness-staging checkout, the D:\Agents\harness-staging
# data root, its own virtual environment and logs, and port 8101. It never reads production config or data.
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'staging-common.ps1')

Assert-StagingTarget -Path $StagingCheckout -Role 'checkout'
Assert-StagingTarget -Path $StagingDataRoot -Role 'data root'
Assert-StagingPort $StagingPort

$configDir = Join-Path $StagingCheckout 'config'
$venv = $StagingVenv
# An optional pointer lets a deploy stage a fresh environment, but only ever inside the staging data root.
$venvPointer = Join-Path $StagingDataRoot '.venv-path'
if (Test-Path -LiteralPath $venvPointer) {
    $pointerValue = Get-Content -Raw -LiteralPath $venvPointer
    if ($null -eq $pointerValue) { throw "empty staging virtual environment pointer: $venvPointer" }
    $venv = ([string]$pointerValue).Trim()
    if (-not [System.IO.Path]::IsPathRooted($venv)) { throw "invalid staging virtual environment pointer: $venvPointer" }
    if (-not (Test-PathInside $venv $StagingDataRoot)) {
        throw "staging virtual environment pointer must stay under ${StagingDataRoot}: $venv"
    }
}
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "staging Python is missing: $python" }

$daemonLog = Join-Path $StagingLogDir 'daemon.log'
$supervisorLog = Join-Path $StagingLogDir 'supervisor.log'
New-Item -ItemType Directory -Force $StagingLogDir | Out-Null

# One staging supervisor per logon session, even if the task is started twice. A distinct mutex name, so starting
# or stopping staging can never interfere with the production supervisor's Local\AgentHarnessDaemon mutex.
$mutex = New-Object System.Threading.Mutex($false, 'Local\AgentHarnessDaemonStaging')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

$shaFile = Join-Path $StagingDataRoot 'deployed-sha.txt'
$commit = if (Test-Path -LiteralPath $shaFile) { ([string](Get-Content -Raw -LiteralPath $shaFile)).Trim() } else { '' }

while ($true) {
    if ((Test-Path $daemonLog) -and (Get-Item $daemonLog).Length -gt 20MB) { Move-Item $daemonLog "$daemonLog.prev" -Force }
    Log "starting staging daemon ($commit)"
    # cmd handles the append redirect so both streams land in one log without PowerShell wrapping stderr.
    $arguments = "/c `"set HARNESS_SUPERVISED=1&& set HARNESS_BUILD_COMMIT=$commit&& " +
        "set HARNESS_CONFIG_DIR=$configDir&& set HARNESS_DATA_DIR=$StagingDataRoot&& " +
        "`"$python`" -u -m harness --config-dir `"$configDir`" --data-dir `"$StagingDataRoot`" >> `"$daemonLog`" 2>&1`""
    $proc = Start-Process cmd.exe -ArgumentList $arguments -WorkingDirectory $StagingCheckout `
        -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    Log "staging daemon exited with code $($proc.ExitCode); restarting in 10s"
    Start-Sleep -Seconds 10
}
