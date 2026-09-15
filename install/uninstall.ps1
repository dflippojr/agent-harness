<#
.SYNOPSIS
Removes an agent harness install's logon tasks and processes. Keeps data, models and config unless -RemoveFiles.
#>
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'agent-harness'),
    [string]$Instance = 'Main',
    [switch]$RemoveFiles
)
$ErrorActionPreference = 'Stop'
$settingsPath = Join-Path $InstallDir 'settings.json'
$s = if (Test-Path $settingsPath) { Get-Content $settingsPath -Raw | ConvertFrom-Json } else { $null }

foreach ($suffix in 'Daemon', 'LlamaServer') {
    $name = "AgentHarness-$Instance-$suffix"
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed task $name"
    }
}
# Supervisors (powershell) and their children: the daemon (python via cmd) and llama-server.
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*$settingsPath*"
}
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($p.Name) $($p.ProcessId)" }
if ($s) {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*") -or
        ($_.Name -eq 'llama-server.exe' -and $s.llama_server -and $_.ExecutablePath -eq $s.llama_server -and $_.CommandLine -like "*--port $($s.port)*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($_.Name) $($_.ProcessId)" }
    if (Test-Path $s.pause_flag) { Remove-Item $s.pause_flag -Force }
}
if ($RemoveFiles) {
    Remove-Item -Recurse -Force $InstallDir
    Write-Host "deleted $InstallDir (Docker image agent-harness-sandbox:py312 left in place: docker rmi agent-harness-sandbox:py312)"
} else {
    Write-Host "kept $InstallDir (data, models, config); run with -RemoveFiles to delete it"
}
