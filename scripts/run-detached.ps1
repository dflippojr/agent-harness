<#
Run a bake-off module as a Task Scheduler job, so it isn't in the caller's process tree and an IDE or agent
watchdog can't kill it partway. Stops the always-on llama-server first (the bake-off needs the whole GPU) and
restarts it afterwards, whether or not the run succeeded.

    .\scripts\run-detached.ps1 -Log runs\openhands-console.log -PythonArgs '-m bakeoff.openhands_ref --models a,b'

Progress: the -Log file. Finished when it ends with "llama-server task restarted".
#>
param(
    [Parameter(Mandatory)] [string] $Log,
    # Passed to python verbatim. Avoid quotes and % in it; they would need cmd escaping.
    [Parameter(Mandatory)] [string] $PythonArgs
)

$root = Split-Path $PSScriptRoot -Parent
$logPath = Join-Path $root $Log
$batch = Join-Path $root "runs\run-detached.cmd"

# A plain batch file avoids nesting PowerShell and cmd quoting. Without `exit /b` on errors, every line runs,
# so the restart happens even if python fails.
@"
@echo off
cd /d "$root"
powershell -NoProfile -Command "Stop-ScheduledTask AgentHarness-LlamaServer; Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force"
timeout /t 3 /nobreak >nul
.venv\Scripts\python.exe -u $PythonArgs >> "$logPath" 2>&1
powershell -NoProfile -Command "Start-ScheduledTask AgentHarness-LlamaServer"
echo llama-server task restarted>> "$logPath"
"@ | Set-Content -Encoding ascii $batch

$action = New-ScheduledTaskAction -Execute cmd.exe -Argument "/c `"$batch`""
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName AgentHarness-Bakeoff -Action $action -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName AgentHarness-Bakeoff
"started scheduled task AgentHarness-Bakeoff; log: $logPath"
