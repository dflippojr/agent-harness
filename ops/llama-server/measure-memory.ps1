# One memory reading of the tower and llama-server, for docs/resource-guard.md (#311). Run it before and after a
# model load, once with the default mmap load and once with '--load-mode none' added to run-qwen.ps1's $serverArgs.
# With -Bench it also sends one fixed prompt and records llama-server's own prompt/decode speed.
# Appends one JSON line to C:\AI\logs\memory-measurements.jsonl and prints it. Changes nothing on the machine.
param(
    [Parameter(Mandatory)][string]$Label,   # e.g. 'mmap-before-load', 'mmap-loaded', 'none-loaded'
    [switch]$Bench,
    [string]$BaseUrl = 'http://127.0.0.1:8090',
    [string]$Out = 'C:\AI\logs\memory-measurements.jsonl'
)
$ErrorActionPreference = 'Stop'

function GB($bytes) { if ($null -eq $bytes) { $null } else { [math]::Round($bytes / 1GB, 2) } }

$counters = '\Memory\Available Bytes', '\Memory\Committed Bytes', '\Memory\Commit Limit',
    '\Memory\Standby Cache Normal Priority Bytes', '\Memory\Standby Cache Reserve Bytes',
    '\Memory\Standby Cache Core Bytes', '\Memory\Modified Page List Bytes'
$sys = @{}
foreach ($s in (Get-Counter $counters).CounterSamples) { $sys[($s.Path -replace '^.*\\memory\\', '')] = GB $s.CookedValue }

$proc = Get-Process llama-server -ErrorAction SilentlyContinue | Select-Object -First 1
$server = if ($proc) {
    @{ pid = $proc.Id; private_gb = GB $proc.PrivateMemorySize64; working_set_gb = GB $proc.WorkingSet64
       peak_working_set_gb = GB $proc.PeakWorkingSet64; started = $proc.StartTime.ToString('s') }
} else { $null }

$sleeping = $null
try { $sleeping = (Invoke-RestMethod "$BaseUrl/props" -TimeoutSec 5).is_sleeping } catch { }

$benchResult = $null
if ($Bench -and $proc) {
    $body = @{ messages = @(@{ role = 'user'; content = 'List the first 40 prime numbers, comma separated.' })
               max_tokens = 256; temperature = 0; chat_template_kwargs = @{ enable_thinking = $false } } | ConvertTo-Json -Depth 5
    $r = Invoke-RestMethod "$BaseUrl/v1/chat/completions" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 600
    $benchResult = @{ prompt_tps = [math]::Round($r.timings.prompt_per_second, 1); decode_tps = [math]::Round($r.timings.predicted_per_second, 1)
                decode_tokens = $r.timings.predicted_n }
}

$row = [ordered]@{ at = (Get-Date).ToString('s'); label = $Label; system_gb = $sys; llama_server = $server
                   is_sleeping = $sleeping; paused_flag = (Test-Path 'C:\AI\llama-server.paused'); bench = $benchResult }
$line = $row | ConvertTo-Json -Depth 5 -Compress
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
Add-Content $Out $line
$line
