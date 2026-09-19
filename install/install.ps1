<#
.SYNOPSIS
Installs the agent harness on a Windows PC. No administrator rights needed.

.DESCRIPTION
Run from a checkout of the repository:

    git clone https://github.com/dflippojr/agent-harness; cd agent-harness
    powershell -ExecutionPolicy Bypass -File install\install.ps1

It checks the machine, creates the Python environment and Docker images, writes a config, registers per-user logon
tasks, starts the daemon, and runs `python -m harness.doctor`. The Full profile also installs llama.cpp and a local
model; Service configures hosted provider CLIs. Downloads resume if interrupted; running it again repairs or updates
an install and keeps the detailed config. See docs/INSTALL.md.

.PARAMETER InstallDir
Where tools, models, config and logs go. Default %LOCALAPPDATA%\agent-harness.
.PARAMETER DataDir
Session database, workspaces and backups. Default <InstallDir>\data.
.PARAMETER Profile
Auto preserves an existing profile and otherwise installs Full. Service runs hosted providers without a local model.
.PARAMETER EnableModules
Optional modules to enable in the Service profile. Pass a comma-separated PowerShell array.
.PARAMETER Model
auto (16 GB+ VRAM: qwen, otherwise gpt-oss), qwen (Qwen3.6-35B-A3B, needs 16 GB VRAM + 32 GB RAM) or gpt-oss
(gpt-oss-20b, 12 GB+ VRAM).
.PARAMETER ModelPath
Use an existing GGUF file instead of downloading.
.PARAMETER LlamaDir
Use an existing llama.cpp CUDA build (folder with llama-server.exe) instead of downloading.
.PARAMETER ExistingServer
URL of a llama-server that's already running (e.g. http://127.0.0.1:8090): skips the model download and the model
server task.
.PARAMETER Instance
Name used in scheduled task names and mutexes, so two installs can coexist. Default Main.
.PARAMETER Port
Daemon port (localhost). Default 8100.
.PARAMETER ServerPort
Model server port (localhost). Default 8090.
.PARAMETER NoTasks
Don't register or start logon tasks (start the daemon yourself).
.PARAMETER DryRun
Print what would happen without changing anything.
.PARAMETER Force
Rewrite the config files even if they exist.
#>
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'agent-harness'),
    [string]$DataDir = '',
    [ValidateSet('Auto', 'Full', 'Service')][string]$Profile = 'Auto',
    [ValidateSet('local_model', 'homelab', 'memory_library', 'images', 'image_edit', 'jobs', 'gpu_guard', 'runners',
                 'remote_control', 'web', 'search', 'endpoint', 'notifications', 'backup')]
    [string[]]$EnableModules = @(),
    [ValidateSet('auto', 'qwen', 'gpt-oss')][string]$Model = 'auto',
    [string]$ModelPath = '',
    [string]$LlamaDir = '',
    [string]$ExistingServer = '',
    [string]$Instance = 'Main',
    [int]$Port = 8100,
    [int]$ServerPort = 8090,
    [switch]$NoTasks,
    [switch]$DryRun,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$AppDir = Split-Path $PSScriptRoot -Parent
if (-not $DataDir) { $DataDir = Join-Path $InstallDir 'data' }
$profilePath = Join-Path $InstallDir 'config\profile.yaml'
$EffectiveProfile = $Profile
if ($EffectiveProfile -eq 'Auto') {
    $EffectiveProfile = if ((Test-Path $profilePath) -and
        (Select-String -Path $profilePath -Pattern '^profile:\s*service\s*$' -Quiet)) { 'Service' } else { 'Full' }
}
$localDependents = @('endpoint', 'images', 'image_edit', 'gpu_guard')
if ($ExistingServer -or $ModelPath -or $LlamaDir -or @($EnableModules | Where-Object { $_ -in $localDependents }).Count) {
    $EnableModules = @($EnableModules + 'local_model' | Select-Object -Unique)
}
if ($EnableModules -contains 'image_edit') {
    $EnableModules = @($EnableModules + 'images' | Select-Object -Unique)
}
$NeedsLocalModel = $EffectiveProfile -eq 'Full' -or $EnableModules -contains 'local_model'
$LlamaBuild = 'b10950'   # tested build (docs/phase0-results.md)
$UvVersion = '0.12.14'
$Models = @{
    'qwen'    = @{ name = 'qwen3.6-35b-a3b'; file = 'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'; gb = 22.4; context = 65536
                   url = 'https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'
                   args = @('--fit', 'on', '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0') }
    'gpt-oss' = @{ name = 'gpt-oss-20b'; file = 'gpt-oss-20b-MXFP4.gguf'; gb = 12.1; context = 32768
                   url = 'https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf'
                   args = @('--n-gpu-layers', '99', '--reasoning-effort', 'medium') }
}

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Warn($msg) { Write-Host "    WARNING: $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }
function Act($description, [scriptblock]$action) {
    if ($DryRun) { Info "[dry run] $description"; return }
    Info $description
    & $action
}
function Download($url, $dest, [double]$gb = 0) {
    if (Test-Path $dest) { Info "already downloaded: $dest"; return }
    Act "download $url$(if ($gb) { " ($gb GB)" })" {
        New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
        # curl.exe ships with Windows 10/11; -C - resumes a partial download.
        & curl.exe -L --fail --retry 5 --retry-delay 10 -C - -o "$dest.part" $url
        if ($LASTEXITCODE -ne 0) { Die "download failed: $url (run the installer again to resume)" }
        Move-Item "$dest.part" $dest -Force
    }
}

Write-Host "Agent harness installer  (app: $AppDir, install: $InstallDir, instance: $Instance, profile: $EffectiveProfile)" -ForegroundColor White

# ---------------------------------------------------------------- checks
Step 'Checking the machine'
if ([Environment]::OSVersion.Version.Major -lt 10 -or -not [Environment]::Is64BitOperatingSystem) {
    Die 'Windows 10 or 11, 64-bit, is required.'
}
$m = $null
if ($NeedsLocalModel) {
    $gpu = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $gpu) { Die 'The local_model module needs an NVIDIA GPU and driver (nvidia-smi failed).' }
    $gpuName, $driver, $vramMiB = ($gpu | Select-Object -First 1).Split(',') | ForEach-Object { $_.Trim() }
    $vramGB = [math]::Round([int]$vramMiB / 1024, 1)
    Info "GPU: $gpuName, $vramGB GB, driver $driver"
    if ([int]($driver.Split('.')[0]) -lt 580) { Die "NVIDIA driver $driver is too old for the CUDA 13 build; update to 580 or newer." }
    $ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    Info "RAM: $ramGB GB"
    if ($Model -eq 'auto') { $Model = if ($vramGB -ge 15.5 -and $ramGB -ge 30) { 'qwen' } else { 'gpt-oss' } }
    if ($vramGB -lt 11.5 -and -not $ExistingServer) { Die "$vramGB GB of VRAM is not enough for the supported models (12 GB+)." }
    if ($Model -eq 'qwen' -and ($vramGB -lt 15.5 -or $ramGB -lt 30)) { Warn 'Qwen3.6-35B-A3B was tested with 16 GB VRAM and 32 GB RAM; expect slowdowns or failures.' }
    $m = $Models[$Model]
    Info "model: $($m.name)"
} else {
    if ($Model -eq 'auto') { $Model = 'gpt-oss' } # setup_config requires a preset but omits it from service config
    Info 'local model: disabled (hosted-provider service profile)'
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Die 'Git is required: winget install Git.Git (then open a new terminal).' }
$dockerVersion = & docker version --format '{{.Server.Version}}' 2>$null
if ($LASTEXITCODE -ne 0 -or -not $dockerVersion) {
    Die 'Docker Desktop must be installed and running (agents run commands in containers): winget install Docker.DockerDesktop'
}
Info "Docker engine $dockerVersion"

$freeGB = [math]::Round((Get-PSDrive (Split-Path $InstallDir -Qualifier).TrimEnd(":")).Free / 1GB)
$needGB = if (-not $NeedsLocalModel -or $ModelPath -or $ExistingServer) { 5 } else { [math]::Ceiling($m.gb) + 5 }
if ($EnableModules -contains 'image_edit') {
    $needGB += 22
    if ($NeedsLocalModel) {
        if ($vramGB -lt 15.5) { Die "image_edit needs about 16 GB of VRAM (this GPU reports $vramGB GB)." }
        if ($ramGB -lt 30) { Warn "image_edit was tested with 32 GB RAM; this machine reports $ramGB GB." }
    }
}
if ($freeGB -lt $needGB) { Die "Only $freeGB GB free on $(Split-Path $InstallDir -Qualifier); need about $needGB GB." }
Info "disk: $freeGB GB free (need about $needGB GB)"

# ---------------------------------------------------------------- tools
$bin = Join-Path $InstallDir 'bin'
$uv = Join-Path $bin 'uv.exe'
Step 'Python environment (uv)'
if (-not (Test-Path $uv)) {
    $zip = Join-Path $InstallDir "downloads\uv-$UvVersion.zip"
    Download "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
    Act "unpack uv to $bin" { New-Item -ItemType Directory -Force $bin | Out-Null; Expand-Archive $zip $bin -Force }
}
$venv = Join-Path $InstallDir 'venv'
$python = Join-Path $venv 'Scripts\python.exe'
Act "create $venv with Python 3.12 and install requirements" {
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir 'python'
    & $uv venv --python 3.12 --allow-existing $venv
    if ($LASTEXITCODE -ne 0) { Die 'uv venv failed' }
    & $uv pip install --python $python -r (Join-Path $AppDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Die 'installing Python packages failed' }
}

$llamaServer = ''
if ($NeedsLocalModel -and -not $ExistingServer) {
    Step "llama.cpp $LlamaBuild (CUDA 13.3)"
    if ($LlamaDir) {
        $llamaServer = Join-Path $LlamaDir 'llama-server.exe'
        if (-not (Test-Path $llamaServer)) { Die "llama-server.exe not found in $LlamaDir" }
        Info "using $llamaServer"
    } else {
        $llamaHome = Join-Path $InstallDir "llama.cpp\$LlamaBuild"
        $llamaServer = Join-Path $llamaHome 'llama-server.exe'
        if (-not (Test-Path $llamaServer)) {
            $base = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaBuild"
            $z1 = Join-Path $InstallDir "downloads\llama-$LlamaBuild-bin-win-cuda-13.3-x64.zip"
            $z2 = Join-Path $InstallDir 'downloads\cudart-llama-bin-win-cuda-13.3-x64.zip'
            Download "$base/llama-$LlamaBuild-bin-win-cuda-13.3-x64.zip" $z1
            Download "$base/cudart-llama-bin-win-cuda-13.3-x64.zip" $z2
            Act "unpack llama.cpp to $llamaHome" {
                New-Item -ItemType Directory -Force $llamaHome | Out-Null
                Expand-Archive $z1 $llamaHome -Force; Expand-Archive $z2 $llamaHome -Force
            }
        }
    }

    Step "Model $($m.name)"
    if ($ModelPath) {
        if (-not (Test-Path $ModelPath)) { Die "model file not found: $ModelPath" }
        Info "using $ModelPath"
    } else {
        $ModelPath = Join-Path $InstallDir "models\$($m.file)"
        Download $m.url $ModelPath $m.gb
    }
}

# ---------------------------------------------------------------- sandbox, config
Step 'Docker sandbox image'
Act 'docker build -t agent-harness-sandbox:py312 sandbox' {
    & docker build -q -t agent-harness-sandbox:py312 (Join-Path $AppDir 'sandbox')
    if ($LASTEXITCODE -ne 0) { Die 'building the sandbox image failed' }
}
if ($EffectiveProfile -eq 'Service') {
    Step 'Hosted-provider CLI image and egress proxies'
    Act 'docker build -t agent-harness-cli:1 -f sandbox/cli.Dockerfile sandbox' {
        & docker build -q -t agent-harness-cli:1 -f (Join-Path $AppDir 'sandbox\cli.Dockerfile') (Join-Path $AppDir 'sandbox')
        if ($LASTEXITCODE -ne 0) { Die 'building the hosted-provider CLI image failed' }
    }
    Act 'create harness-egress network and start provider allowlist proxies' {
        & docker network inspect harness-egress *> $null
        if ($LASTEXITCODE -ne 0) { & docker network create harness-egress | Out-Null }
        & docker compose -f (Join-Path $AppDir 'ops\egress\compose.yaml') up -d --build
        if ($LASTEXITCODE -ne 0) { Die 'starting the provider egress proxies failed' }
    }
}

Step 'Configuration'
$configDir = Join-Path $InstallDir 'config'
$logDir = Join-Path $InstallDir 'logs'
$pauseFlag = Join-Path $InstallDir 'llama-server.paused'
$serverUrl = if ($ExistingServer) { $ExistingServer.TrimEnd('/') } else { "http://127.0.0.1:$ServerPort" }
# Same extra_model_paths root as harness.config.DEFAULT_IMAGES_MODELS_DIR / images.models_dir.
$DefaultImagesModelsDir = 'C:\AI\comfy-models'
$modelsRoot = if (Test-Path -LiteralPath $DefaultImagesModelsDir) { $DefaultImagesModelsDir } else { Join-Path $InstallDir 'comfy-models' }
$modelsRootPosix = ([string]$modelsRoot).Replace('\', '/')
Act "write config in $configDir" {
    $genArgs = @('-m', 'harness.setup_config', '--config-dir', $configDir, '--data-dir', $DataDir, '--model', $Model,
        '--profile', $EffectiveProfile.ToLowerInvariant(), '--port', "$Port", '--llama-url', $serverUrl,
        '--pause-flag', $pauseFlag)
    foreach ($module in $EnableModules) { $genArgs += @('--enable-module', $module) }
    if ($ExistingServer -or -not $NeedsLocalModel) { $genArgs += '--no-gpu-guard' }
    if ($Force) { $genArgs += '--force' }
    if ($EnableModules -contains 'image_edit' -and $modelsRootPosix -ne 'C:/AI/comfy-models') {
        $genArgs += @('--images-models-dir', $modelsRoot)
    }
    Push-Location $AppDir; try { & $python @genArgs } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Die 'writing the config failed' }
}
$settingsPath = Join-Path $InstallDir 'settings.json'
$settings = [ordered]@{
    instance = $Instance; app_dir = $AppDir; config_dir = $configDir; python = $python; log_dir = $logDir
    llama_server = $llamaServer; model_path = $ModelPath; model_name = $(if ($m) { $m.name } else { '' }); port = $ServerPort
    context_tokens = $(if ($m) { $m.context } else { 0 }); sleep_idle_seconds = 1800
    extra_args = $(if ($m) { $m.args } else { @() }); pause_flag = $pauseFlag
    existing_server = $ExistingServer
}
Act "write $settingsPath" {
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $settings | ConvertTo-Json | Set-Content -Encoding utf8 $settingsPath
}

# ---------------------------------------------------------------- autostart
if (-not $NoTasks) {
    Step 'Logon tasks'
    if (-not $NeedsLocalModel) {
        $oldModelTask = "AgentHarness-$Instance-LlamaServer"
        if (Get-ScheduledTask -TaskName $oldModelTask -ErrorAction SilentlyContinue) {
            Act "stop and unregister disabled $oldModelTask" {
                Stop-ScheduledTask -TaskName $oldModelTask -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $oldModelTask -Confirm:$false
            }
        }
    }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
    $tasks = @(@{ name = "AgentHarness-$Instance-Daemon"; script = 'run-daemon.ps1'; desc = "Agent harness daemon (127.0.0.1:$Port)" })
    if ($NeedsLocalModel -and -not $ExistingServer) {
        $tasks = @(@{ name = "AgentHarness-$Instance-LlamaServer"; script = 'run-server.ps1'; desc = "Agent harness model server (127.0.0.1:$ServerPort)" }) + $tasks
    }
    foreach ($t in $tasks) {
        Act "register and start $($t.name)" {
            $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -NonInteractive -WindowStyle Hidden " +
                "-ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot $t.script)`" -Settings `"$settingsPath`"")
            Register-ScheduledTask -TaskName $t.name -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
                -Settings $taskSettings -Description $t.desc -Force | Out-Null
            Start-ScheduledTask -TaskName $t.name
        }
    }
    if (-not $DryRun) {
        Info 'waiting for the daemon to answer (the model loads in the background; the first load can take minutes)'
        foreach ($i in 1..60) {
            Start-Sleep -Seconds 2
            try { if ((Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3).ok) { Info "daemon up after $($i * 2) s"; break } } catch { }
        }
    }
}

# ---------------------------------------------------------------- optional Qwen-Image-Edit weights (never part of an ordinary install)
if ($EnableModules -contains 'image_edit') {
    Step 'Optional image-edit component (Qwen-Image-Edit, Apache 2.0)'
    $editRev = '7d41107b653d3039be20972fb82398b01b3213eb'
    $editSha = '393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021'
    $editUrl = "https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/$editRev/split_files/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors"
    $editDest = Join-Path $modelsRoot 'diffusion_models\qwen_image_edit_fp8_e4m3fn.safetensors'
    Info "artifact sha256 $editSha (revision $editRev)"
    if ($DryRun) {
        Info "[dry run] download $editUrl -> $editDest"
    } else {
        Download $editUrl $editDest 19.0
        $hash = (Get-FileHash -Algorithm SHA256 $editDest).Hash.ToLowerInvariant()
        if ($hash -ne $editSha) { Die "image-edit checksum mismatch: got $hash want $editSha" }
        Info "verified $editDest"
    }
}

# ---------------------------------------------------------------- doctor
Step 'Checking the install (python -m harness.doctor)'
if ($DryRun) { Info '[dry run] skipped' } else {
    Push-Location $AppDir
    $doctorArgs = @('-m', 'harness.doctor', '--config-dir', $configDir, '--instance', $(if ($NoTasks) { '' } else { $Instance }))
    if ($ExistingServer -or -not $NeedsLocalModel) { $doctorArgs += '--existing-server' }
    try { & $python @doctorArgs } finally { Pop-Location }
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "  Web app:   http://127.0.0.1:$Port   (phone access: docs/INSTALL.md, 'Use it from your phone')"
Write-Host "  Config:    $configDir"
Write-Host "  Logs:      $logDir"
if ($EffectiveProfile -eq 'Service') {
    Write-Host "  Next:      ops\backends\login.ps1 claude|codex|cursor (run once per provider you use)"
}
if ($NoTasks) {
    Write-Host "  Start:     set HARNESS_CONFIG_DIR=$configDir; `"$python`" -m harness   (from $AppDir)"
}
