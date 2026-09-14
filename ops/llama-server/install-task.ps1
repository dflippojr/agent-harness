# Registers a per-user logon task that runs run-qwen.ps1 hidden. No elevation needed.
# The tower auto-logs-in on boot, so this fires on every restart.
$taskName = 'AgentHarness-LlamaServer'
$script = Join-Path $PSScriptRoot 'run-qwen.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Always-on Qwen llama-server for the local agent harness (127.0.0.1:8090).' -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
