# Restart the harness daemon to pick up code or config changes. Sessions that were running resume on start.
# Stop-ScheduledTask alone isn't enough: it ends the supervisor but leaves the daemon (a cmd.exe child) running,
# still holding port 8100. Killing the daemon lets the running supervisor start a fresh one within ~10 s.
$ErrorActionPreference = 'Stop'

$daemons = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m harness(\s|$)' }
$supervisor = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-daemon\.ps1' }
if (-not $supervisor) {
    Write-Host 'No supervisor running; starting the AgentHarness-Daemon task.'
    $daemons | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask AgentHarness-Daemon
} else {
    $daemons | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

foreach ($i in 1..30) {
    Start-Sleep -Seconds 2
    try {
        if ((Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 3).ok) { Write-Host "daemon up after $($i * 2) s"; exit 0 }
    } catch { }
}
Write-Error 'daemon did not come back within 60 s; see D:\Agents\harness\logs'
