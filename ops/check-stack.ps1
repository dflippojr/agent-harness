# Post-reboot check for the agent harness stack. Read-only.
#   powershell -ExecutionPolicy Bypass -File D:\Projects\agent-harness\ops\check-stack.ps1

function Check($name, [scriptblock]$test) {
    try {
        $detail = & $test
        Write-Host ("[ OK ] {0}  {1}" -f $name, $detail) -ForegroundColor Green
    } catch {
        Write-Host ("[FAIL] {0}  {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    }
}
function Get-Json($url) { Invoke-RestMethod -Uri $url -TimeoutSec 5 }

Check 'Memory speed (XMP)' {
    $speeds = Get-CimInstance Win32_PhysicalMemory | ForEach-Object { "$($_.ConfiguredClockSpeed)/$($_.Speed) MHz" }
    "configured/rated: $($speeds -join ', ')"
}
Check 'Page file' {
    $pf = Get-CimInstance Win32_PageFileUsage
    $os = Get-CimInstance Win32_OperatingSystem
    "{0} allocated {1} MB; commit limit {2:N1} GB" -f $pf.Name, $pf.AllocatedBaseSize, ($os.TotalVirtualMemorySize / 1MB)
}
Check 'Scheduled tasks' {
    $tasks = 'AgentHarness-LlamaServer', 'AgentHarness-Daemon' | ForEach-Object { "$_=$((Get-ScheduledTask $_).State)" }
    $bad = $tasks | Where-Object { $_ -notmatch '=Running' }
    if ($bad) { throw ($tasks -join ', ') }
    $tasks -join ', '
}
Check 'llama-server :8090' {
    $p = Get-Json 'http://127.0.0.1:8090/props'
    "model $($p.model_alias), sleeping=$($p.is_sleeping), n_ctx $($p.default_generation_settings.n_ctx)"
}
Check 'Docker' {
    $v = docker version --format '{{.Server.Version}}' 2>$null
    if (-not $v) { throw 'Docker engine not reachable (is Docker Desktop running?)' }
    $names = docker ps --format '{{.Names}}'
    "engine $v; $(@($names).Count) containers running"
}
Check 'ntfy :8095' {
    $h = Get-Json 'http://127.0.0.1:8095/v1/health'
    if (-not $h.healthy) { throw 'unhealthy' }
    'healthy'
}
Check 'SearXNG :8888' {
    $r = Get-Json 'http://127.0.0.1:8888/search?q=test&format=json'
    "$(@($r.results).Count) results for a test query"
}
Check 'Image generation' {
    $s = (Get-Json 'http://127.0.0.1:8100/images?limit=1').status
    $files = 'diffusion_models\z_image_turbo_bf16.safetensors', 'diffusion_models\qwen_image_2512_fp8_e4m3fn.safetensors' |
        Where-Object { -not (Test-Path (Join-Path 'C:\AI\comfy-models' $_)) }
    if ($files) { throw "missing models: $($files -join ', ')" }
    if (-not (Test-Path 'C:\AI\ComfyUI\python_embeded\python.exe')) { throw 'ComfyUI portable missing at C:\AI\ComfyUI' }
    "phase $($s.phase); models present; ComfyUI starts on demand"
}
# Optional component: warn (never FAIL) when flux-fast assets or nodes are missing.
try {
    $flux = @((Get-Json 'http://127.0.0.1:8100/images?limit=1').status.modes | Where-Object { $_.key -eq 'flux-fast' })[0]
    if (-not $flux) { throw 'flux-fast mode missing from /images status' }
    if ($flux.available) {
        Write-Host ("[ OK ] FLUX.2 klein 4B (optional)  {0}" -f $flux.display_name) -ForegroundColor Green
    } else {
        Write-Host ("[WARN] FLUX.2 klein 4B (optional)  {0}. {1}" -f $flux.unavailable_reason, $flux.remediation) -ForegroundColor Yellow
    }
} catch {
    Write-Host ("[WARN] FLUX.2 klein 4B (optional)  {0}" -f $_.Exception.Message) -ForegroundColor Yellow
}
Check 'Harness daemon :8100' {
    $null = Get-Json 'http://127.0.0.1:8100/health'
    $m = Get-Json 'http://127.0.0.1:8100/models/status'
    "up; model state: $($m[0].state)"
}
Check 'GPU guard' {
    $g = Get-Json 'http://127.0.0.1:8100/gpu'
    $flag = Test-Path 'C:\AI\llama-server.paused'
    $detail = "state $($g.state); triggers: $((@($g.signals) | ForEach-Object { $_.detail }) -join ', ')"
    if ($flag -and $g.state -eq 'clear') { throw "pause flag C:\AI\llama-server.paused exists but the guard is clear; $detail" }
    $detail
}
Check 'Backup' {
    $b = (Get-Json 'http://127.0.0.1:8100/maintenance').backup
    if (-not $b.enabled) { 'disabled' }
    elseif (-not $b.ok_at) { throw "no backup yet $($b.error)" }
    else {
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $b.ok_at
        if ($age -gt 93600) { throw ("last good backup {0:N0} h ago; {1}" -f ($age / 3600), $b.error) }
        "{0:N0} h ago, {1:N1} MB in {2}" -f ($age / 3600), ($b.bytes / 1MB), $b.path
    }
}
Check 'Tailscale serve' {
    $status = & 'C:\Program Files\Tailscale\tailscale.exe' serve status 2>&1 | Out-String
    if ($status -notmatch '8100' -or $status -notmatch '8095') { throw "serve config missing: $status" }
    'https :443 -> daemon, :8443 -> ntfy'
}
Check 'MacBook runner' {
    # Informational: the Mac is often asleep or away, which isn't a tower problem.
    $r = (Get-Json 'http://127.0.0.1:8100/runners') | Where-Object { $_.name -eq 'macbook' }
    if (-not $r) { 'not configured' }
    elseif ($r.online) { "online; runner $($r.info.version), $($r.info.free_gb) GB free on the Mac" }
    else { 'offline or asleep (sessions for it wait until it connects)' }
}
Check 'Grafana / Prometheus' {
    $null = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:9090/-/ready' -TimeoutSec 5
    $null = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:3000/api/health' -TimeoutSec 5
    $t = (Get-Json 'http://127.0.0.1:9090/api/v1/targets?state=active').data.activeTargets |
        Where-Object { $_.labels.job -eq 'agent_harness' }
    if (-not $t -or $t.health -ne 'up') { throw "Prometheus agent_harness target: $($t.health) $($t.lastError)" }
    'both ready; agent_harness target up'
}
