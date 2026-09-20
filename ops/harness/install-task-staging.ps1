# Registers the per-user logon task that runs run-daemon-staging.ps1 hidden (issue #129). No elevation needed.
# Separate task name and script from production: this never edits or replaces AgentHarness-Daemon.
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'staging-common.ps1')

Assert-StagingTask $StagingTaskName
$script = Join-Path $PSScriptRoot 'run-daemon-staging.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "staging supervisor is missing: $script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

Register-ScheduledTask -TaskName $StagingTaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Agent harness staging smoke slot (127.0.0.1:$StagingPort), published on the tailnet at :$StagingServePort." `
    -Force | Out-Null
Get-ScheduledTask -TaskName $StagingTaskName | Select-Object TaskName, State
