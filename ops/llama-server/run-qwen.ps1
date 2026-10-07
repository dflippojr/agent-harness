# Supervisor for the always-on Qwen llama-server. Started hidden at logon by the
# "AgentHarness-LlamaServer" scheduled task (see install-task.ps1). Restarts the server if it exits.
# At logon it parks the server (creates the pause flag) instead of loading the model: the harness daemon removes the
# flag when something needs the model (docs/resource-guard.md). -LoadAtLogon restores the old eager start.
# It serves the harness's default_model (see $localModels); -Model pins one instead.
param([switch]$LoadAtLogon, [string]$Model = '')
$ErrorActionPreference = 'Stop'

$exe = 'C:\AI\llama.cpp\b10950\llama-server.exe'
$logDir = 'C:\AI\logs'
$serverLog = Join-Path $logDir 'llama-server-qwen.log'
$supervisorLog = Join-Path $logDir 'llama-server-supervisor.log'

# The local models this server can run, keyed by their name in config/harness.yaml `models`. The supervisor serves
# the harness's default_model, read again before every server start: config/harness.local.yaml wins over
# config/harness.yaml. So switching models, and rolling back, is that one config line (docs/INSTALL.md, "Switch the
# local model"). A name not listed here, or a missing file, falls back to $fallbackModel.
$localModels = [ordered]@{
    'qwen3.6-35b-a3b'         = @{ path = 'C:/AI/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'; args = @() }
    # empero-ai/Qwen3.8-35B-A3B-Distill-GGUF, revision b1f9d1dcc3de8aa867669b0ab919384aeeb9b8d5, SHA-256
    # 196103269085bc54c9b8f49ed21e9f53e1b56b465e8b796c6d8e31e06f63cfa5 (docs/qwen38-distill-study.md, #174, #413).
    'qwen3.8-35b-a3b-distill' = @{ path = 'C:/AI/models/Qwen3.8-35B-A3B-Q4_K_M.gguf'; args = @() }
}
$fallbackModel = 'qwen3.6-35b-a3b'
$configDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\config"))

$commonArgs = @(
    '--host', '127.0.0.1',            # never exposed; Prometheus reaches it via host.docker.internal
    '--port', '8090',
    '--ctx-size', '65536',             # 64K costs ~2% decode vs 32K with no extra VRAM/RAM (docs/phase0-results.md)
    '--fit', 'on',
    '--flash-attn', 'on',
    '--cache-type-k', 'q8_0',
    '--cache-type-v', 'q8_0',
    # Read the weights at load instead of mmap: the first prompt after a load ran at 149-173 tok/s while mmap paged
    # the CPU experts in; with 'none' it runs at ~1,150 tok/s, loads in 14 s instead of 40, and leaves more RAM
    # available. It costs ~8 GB more commit charge (docs/resource-guard.md, #405).
    '--load-mode', 'none',
    '--parallel', '1',
    '--jinja',
    '--metrics',
    # Unload after 10 idle minutes; the next request reloads it (~1 min). If you change this, also change the
    # sleep_after constant in the Grafana "Local LLM (llama-server)" dashboard (observability-stack repo) and
    # gpu_guard.keepalive_seconds in config/harness.yaml (it must stay below this).
    '--sleep-idle-seconds', '600'
)

New-Item -ItemType Directory -Force $logDir | Out-Null

# One supervisor per session, even if the task is started twice.
$mutex = New-Object System.Threading.Mutex($false, 'Local\AgentHarnessLlamaServer')
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }

# The first top-level default_model in harness.local.yaml, then harness.yaml; '' when neither has one.
function Get-ConfiguredModel {
    foreach ($name in 'harness.local.yaml', 'harness.yaml') {
        $file = Join-Path $configDir $name
        if (-not (Test-Path $file)) { continue }
        $match = Select-String -Path $file -Pattern '^default_model:\s*["'']?([^"''\s#]+)' | Select-Object -First 1
        if ($match) { return $match.Matches[0].Groups[1].Value }
    }
    return ''
}

function Get-ServerArgs {
    $name = if ($Model) { $Model } else { Get-ConfiguredModel }
    if (-not $localModels.Contains($name)) {
        Log "model '$name' is not in run-qwen.ps1's list; serving $fallbackModel"
        $name = $fallbackModel
    } elseif (-not (Test-Path $localModels[$name].path)) {
        Log "model file $($localModels[$name].path) is missing; serving $fallbackModel"
        $name = $fallbackModel
    }
    $spec = $localModels[$name]
    return @('-m', $spec.path, '--alias', $name) + $commonArgs + $spec.args
}

# The harness daemon's resource guard (harness/gpu_guard.py) creates this file and stops the server while a game or
# a Plex hardware transcode needs the GPU, and leaves it in place afterwards until something needs the model.
# Don't start the server until the file is gone. Keep in sync with gpu_guard.pause_flag in config/harness.yaml.
$pauseFlag = 'C:\AI\llama-server.paused'
$loggedPause = $false

if (-not $LoadAtLogon -and -not (Test-Path $pauseFlag)) {
    "parked at logon by run-qwen.ps1 at $(Get-Date -Format s)" | Set-Content $pauseFlag
    Log 'parked at logon; the harness loads the model when something needs it'
}

while ($true) {
    if (Test-Path $pauseFlag) {
        if (-not $loggedPause) { Log 'paused or parked by the harness resource guard; waiting for the pause flag to go'; $loggedPause = $true }
        Start-Sleep -Seconds 5
        continue
    }
    if ($loggedPause) { Log 'pause flag removed'; $loggedPause = $false }
    if (Test-Path $serverLog) { Move-Item $serverLog "$serverLog.prev" -Force }
    $serverArgs = Get-ServerArgs
    Log "starting llama-server ($($serverArgs[3]))"
    $proc = Start-Process $exe -ArgumentList $serverArgs -WindowStyle Hidden -PassThru -RedirectStandardError $serverLog
    $proc.WaitForExit()
    Log "llama-server exited with code $($proc.ExitCode); restarting in 15s"
    Start-Sleep -Seconds 15
}
