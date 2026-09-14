# Registers a per-user logon task that runs run-daemon.ps1 hidden. No elevation needed.
# The tower auto-logs-in on boot, so this fires on every restart.
$taskName = 'AgentHarness-Daemon'
$script = Join-Path $PSScriptRoot 'run-daemon.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Agent harness daemon (127.0.0.1:8100), published on the tailnet by tailscale serve.' -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
