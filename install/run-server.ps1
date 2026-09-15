# Supervisor for the model server of an installed agent harness. Started hidden at logon by the
# "AgentHarness-<Instance>-LlamaServer" task that install.ps1 registers. Reads settings.json next to the install's
# config and restarts llama-server if it exits. While the GPU guard's pause flag exists (a game, a Plex transcode, or
# image generation needs the GPU) it waits instead of restarting.
param([Parameter(Mandatory)][string]$Settings)
$ErrorActionPreference = 'Stop'

$s = Get-Content $Settings -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force $s.log_dir | Out-Null
$serverLog = Join-Path $s.log_dir 'llama-server.log'
$supervisorLog = Join-Path $s.log_dir 'llama-server-supervisor.log'

$serverArgs = @('-m', $s.model_path, '--alias', $s.model_name, '--host', '127.0.0.1', '--port', "$($s.port)",
    '--ctx-size', "$($s.context_tokens)", '--flash-attn', 'on', '--parallel', '1', '--jinja', '--metrics',
    '--sleep-idle-seconds', "$($s.sleep_idle_seconds)") + @($s.extra_args)

# One supervisor per install, even if the task is started twice.
$mutex = New-Object System.Threading.Mutex($false, "Local\AgentHarness-$($s.instance)-LlamaServer")
if (-not $mutex.WaitOne(0)) { exit 0 }

function Log($msg) { "$(Get-Date -Format s) $msg" | Add-Content $supervisorLog }
$loggedPause = $false

while ($true) {
    if (Test-Path $s.pause_flag) {
        if (-not $loggedPause) { Log 'paused by the harness GPU guard; waiting for the pause flag to go'; $loggedPause = $true }
        Start-Sleep -Seconds 5
        continue
    }
    if ($loggedPause) { Log 'pause flag removed'; $loggedPause = $false }
    if (Test-Path $serverLog) { Move-Item $serverLog "$serverLog.prev" -Force }
    Log 'starting llama-server'
    $proc = Start-Process $s.llama_server -ArgumentList $serverArgs -WindowStyle Hidden -PassThru -RedirectStandardError $serverLog
    $proc.WaitForExit()
    Log "llama-server exited with code $($proc.ExitCode); restarting in 15s"
    Start-Sleep -Seconds 15
}
