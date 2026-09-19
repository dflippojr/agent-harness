# Restart, stop, or start the harness daemon. Sessions that were running resume on start.
param([ValidateSet('Restart', 'Stop', 'Start')][string]$Mode = 'Restart')
$ErrorActionPreference = 'Stop'

function Get-HarnessDaemonProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '-m harness(\s|$)') -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-daemon\.ps1')
    }
}

function Stop-HarnessDaemon {
    $taskStopError = $null
    try {
        $task = Get-ScheduledTask -TaskName AgentHarness-Daemon
        if ($task.State -eq 'Running') {
            try {
                Stop-ScheduledTask -TaskName AgentHarness-Daemon
            } catch {
                # A concurrent stop is success. Preserve every other scheduled-task failure.
                try { $taskAfterFailure = Get-ScheduledTask -TaskName AgentHarness-Daemon }
                catch { $taskAfterFailure = $null }
                if ($null -eq $taskAfterFailure -or $taskAfterFailure.State -eq 'Running') {
                    $taskStopError = $_
                }
            }
        }
    } catch {
        $taskStopError = $_
    }

    # Stopping the task ends the supervisor, but descendants can survive and retain port 8100.
    foreach ($attempt in 1..30) {
        $remaining = @(Get-HarnessDaemonProcesses)
        if ($remaining.Count -eq 0) { break }
        $remaining | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    $remaining = @(Get-HarnessDaemonProcesses)
    if ($remaining) { throw 'could not stop all harness daemon processes' }
    if ($null -ne $taskStopError) { throw "could not stop AgentHarness-Daemon scheduled task: $($taskStopError.Exception.Message)" }
    Write-Host 'AgentHarness-Daemon stopped.'
}

function Start-HarnessDaemon {
    $task = Get-ScheduledTask -TaskName AgentHarness-Daemon
    if ($task.State -ne 'Running') {
        try {
            Start-ScheduledTask -TaskName AgentHarness-Daemon
        } catch {
            # A concurrent start is success. Preserve every other scheduled-task failure.
            $taskAfterFailure = Get-ScheduledTask -TaskName AgentHarness-Daemon
            if ($taskAfterFailure.State -ne 'Running') { throw }
        }
    }
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

if ($MyInvocation.InvocationName -ne '.') {
    if ($Mode -in @('Restart', 'Stop')) { Stop-HarnessDaemon }
    if ($Mode -in @('Restart', 'Start')) { Start-HarnessDaemon }
}
