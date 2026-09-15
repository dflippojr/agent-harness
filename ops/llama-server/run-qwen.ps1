# Supervisor for the always-on Qwen llama-server. Started hidden at logon by the
# "AgentHarness-LlamaServer" scheduled task (see install-task.ps1). Restarts the server if it exits.
$ErrorActionPreference = 'Stop'

$exe = 'C:\AI\llama.cpp\b10950\llama-server.exe'
$logDir = 'C:\AI\logs'
$serverLog = Join-Path $logDir 'llama-server-qwen.log'
$supervisorLog = Join-Path $logDir 'llama-server-supervisor.log'

$serverArgs = @(
    '-m', 'C:/AI/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf',
    '--alias', 'qwen3.6-35b-a3b',
    '--host', '127.0.0.1',            # never exposed; Prometheus reaches it via host.docker.internal
    '--port', '8090',
    '--ctx-size', '65536',             # 64K costs ~2% decode vs 32K with no extra VRAM/RAM (docs/phase0-results.md)
    '--fit', 'on',
    '--flash-attn', 'on',
    '--cache-type-k', 'q8_0',
    '--cache-type-v', 'q8_0',
    '--parallel', '1',
    '--jinja',
    '--metrics',
    # Unload after 30 idle minutes; the next request reloads it. If you change this, also change the
    # sleep_after constant in the Grafana "Local LLM (llama-server)" dashboard (observability-stack repo).
    '--sleep-idle-seconds', '1800'
)

New-Item -ItemType Directory -Force $logDir | Out-Null

# One supervisor per session, even if the task is started twice.
$mutex = New-Object System.Threading.Mutex($false, 'Local\AgentHarnessLlamaServer')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

while ($true) {
    if (Test-Path $serverLog) { Move-Item $serverLog "$serverLog.prev" -Force }
    Log 'starting llama-server'
    $proc = Start-Process $exe -ArgumentList $serverArgs -WindowStyle Hidden -PassThru -RedirectStandardError $serverLog
    $proc.WaitForExit()
    Log "llama-server exited with code $($proc.ExitCode); restarting in 15s"
    Start-Sleep -Seconds 15
}
