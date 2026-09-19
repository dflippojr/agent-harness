# Supervisor for the daemon of an installed agent harness. Started hidden at logon by the
# "AgentHarness-<Instance>-Daemon" task that install.ps1 registers. Restarts the daemon if it exits; sessions resume.
# Sets HARNESS_SUPERVISED=1 so the daemon can advertise a supervised restart.
param([Parameter(Mandatory)][string]$Settings)
$ErrorActionPreference = 'Stop'

$s = Get-Content $Settings -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force $s.log_dir | Out-Null
$daemonLog = Join-Path $s.log_dir 'daemon.log'
$supervisorLog = Join-Path $s.log_dir 'daemon-supervisor.log'

$mutex = New-Object System.Threading.Mutex($false, "Local\AgentHarness-$($s.instance)-Daemon")
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

while ($true) {
    if ((Test-Path $daemonLog) -and (Get-Item $daemonLog).Length -gt 20MB) { Move-Item $daemonLog "$daemonLog.prev" -Force }
    Log 'starting daemon'
    # cmd handles the append redirect so both streams land in one log.
    $cmd = "/c set `"HARNESS_CONFIG_DIR=$($s.config_dir)`" && set `"HARNESS_SUPERVISED=1`" && `"$($s.python)`" -u -m harness >> `"$daemonLog`" 2>&1"
    $proc = Start-Process cmd.exe -ArgumentList $cmd -WorkingDirectory $s.app_dir -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    Log "daemon exited with code $($proc.ExitCode); restarting in 10s"
    Start-Sleep -Seconds 10
}
