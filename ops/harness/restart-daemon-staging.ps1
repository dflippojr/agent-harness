# Restart, stop, or start the staging harness daemon (issue #129). Staging only: the scheduled task
# AgentHarness-Daemon-Staging, port 8101, and processes whose command line names the staging data root.
# Production's AgentHarness-Daemon and port 8100 are never matched here, by name prefix or otherwise.
param([ValidateSet('Restart', 'Stop', 'Start')][string]$Mode = 'Restart')
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'staging-common.ps1')

Assert-StagingTask $StagingTaskName
Assert-StagingPort $StagingPort

function Get-StagingDaemonProcesses {
    # 'harness-staging' appears in every staging command line through --data-dir / --config-dir and the supervisor
    # path. A production daemon command line never contains it, so production is out of reach of this matcher.
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match '-m harness(\s|$)' -and
            $_.CommandLine -match 'harness-staging') -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-daemon-staging\.ps1')
    }
}

function Stop-StagingDaemon {
    $taskStopError = $null
    try {
        $task = Get-ScheduledTask -TaskName $StagingTaskName -ErrorAction Stop
        if ($task.State -eq 'Running') {
            try {
                Stop-ScheduledTask -TaskName $StagingTaskName
            } catch {
                # A concurrent stop is success. Preserve every other scheduled-task failure.
                try { $taskAfterFailure = Get-ScheduledTask -TaskName $StagingTaskName }
                catch { $taskAfterFailure = $null }
                if ($null -eq $taskAfterFailure -or $taskAfterFailure.State -eq 'Running') { $taskStopError = $_ }
            }
        }
    } catch {
        # The slot may never have been installed. That is a stopped slot, not a failure.
        $taskStopError = $null
    }

    foreach ($attempt in 1..30) {
        $remaining = @(Get-StagingDaemonProcesses)
        if ($remaining.Count -eq 0) { break }
        $remaining | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    $remaining = @(Get-StagingDaemonProcesses)
    if ($remaining) { throw 'could not stop all staging harness daemon processes' }
    if ($null -ne $taskStopError) {
        throw "could not stop $StagingTaskName scheduled task: $($taskStopError.Exception.Message)"
    }
    Write-Host "$StagingTaskName stopped."
}

function Start-StagingDaemon {
    $task = Get-ScheduledTask -TaskName $StagingTaskName -ErrorAction Stop
    if ($task.State -ne 'Running') {
        try {
            Start-ScheduledTask -TaskName $StagingTaskName
        } catch {
            # A concurrent start is success. Preserve every other scheduled-task failure.
            $taskAfterFailure = Get-ScheduledTask -TaskName $StagingTaskName
            if ($taskAfterFailure.State -ne 'Running') { throw }
        }
    }
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        try {
            if ((Invoke-RestMethod "http://127.0.0.1:$StagingPort/health" -TimeoutSec 3).ok) {
                Write-Host "staging daemon up after $($i * 2) s"
                return
            }
        } catch { }
    }
    throw "staging daemon did not come back within 60 s; see $StagingLogDir"
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($Mode -in @('Restart', 'Stop')) { Stop-StagingDaemon }
    if ($Mode -in @('Restart', 'Start')) { Start-StagingDaemon }
}
