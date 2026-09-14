# Supervisor for the harness daemon. Started hidden at logon by the "AgentHarness-Daemon" scheduled task
# (see install-task.ps1). Restarts the daemon if it exits. Sessions that were running resume on restart.
$ErrorActionPreference = 'Stop'

$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python = Join-Path $root '.venv\Scripts\python.exe'
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
    $proc = Start-Process cmd.exe -ArgumentList "/c `"`"$python`" -u -m harness >> `"$daemonLog`" 2>&1`"" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    Log "daemon exited with code $($proc.ExitCode); restarting in 10s"
    Start-Sleep -Seconds 10
}
