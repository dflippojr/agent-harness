# Restart, stop, or start the harness daemon. Sessions that were running resume on start.
param([ValidateSet('Restart', 'Stop', 'Start')][string]$Mode = 'Restart')
$ErrorActionPreference = 'Stop'

function Stop-HarnessDaemon {
    # Stopping the task ends the supervisor, but its cmd/python descendants can survive and retain port 8100.
    Stop-ScheduledTask -TaskName AgentHarness-Daemon
    $processes = Get-CimInstance Win32_Process
    $daemons = $processes | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m harness(\s|$)' }
    $supervisors = $processes | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-daemon\.ps1' }
    $daemons | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $supervisors | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    $remaining = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '-m harness(\s|$)') -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-daemon\.ps1')
    }
    if ($remaining) { throw 'could not stop all harness daemon processes' }
    Write-Host 'AgentHarness-Daemon stopped.'
}

function Start-HarnessDaemon {
    $task = Get-ScheduledTask -TaskName AgentHarness-Daemon
    if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName AgentHarness-Daemon }
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        try {
            if ((Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 3).ok) {
                Write-Host "daemon up after $($i * 2) s"
                return
            }
        } catch { }
    }
    throw 'daemon did not come back within 60 s; see D:\Agents\harness\logs'
}

if ($Mode -in @('Restart', 'Stop')) { Stop-HarnessDaemon }
if ($Mode -in @('Restart', 'Start')) { Start-HarnessDaemon }
