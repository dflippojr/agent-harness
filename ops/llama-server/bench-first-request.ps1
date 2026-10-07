# First-request timing after a cold load, for docs/resource-guard.md (#405). Starts a throwaway llama-server with
# run-qwen.ps1's model and args on a spare port, waits for /health, records a measure-memory.ps1 row, then sends a
# fixed prompt of exactly -Tokens tokens as the first request and again as a second one (no prompt cache), and stops
# the server. A watchdog job kills the server if available RAM falls under -FloorGB.
# Stop the production server first (harness gpu pause): two copies of the model don't fit in RAM or VRAM.
# Prints one JSON line and appends it to C:\AI\logs\first-request-measurements.jsonl.
param(
    [Parameter(Mandatory)][string]$Label,   # e.g. 'mmap', 'none'
    [string[]]$Extra = @(),                 # e.g. '--load-mode','none'
    [int]$Port = 8097,
    [int]$Tokens = 6360,                    # the chat template adds 12: 6,372 prompt tokens, like the 6,388 in the logs
    [double]$FloorGB = 2.0,
    [string]$Out = 'C:\AI\logs\first-request-measurements.jsonl'
)
$ErrorActionPreference = 'Stop'

$exe = 'C:\AI\llama.cpp\b10950\llama-server.exe'
$base = "http://127.0.0.1:$Port"
$measure = Join-Path $PSScriptRoot 'measure-memory.ps1'
$docs = Join-Path $PSScriptRoot '..\..\docs'

if (Get-Process llama-server -ErrorAction SilentlyContinue) { throw 'a llama-server is already running; park it first' }

function Post($path, $obj) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Depth 6 -Compress))
    Invoke-RestMethod "$base$path" -Method Post -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 900
}
function Chat($text) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Post '/v1/chat/completions' @{ messages = @(@{ role = 'user'; content = $text }); max_tokens = 16; temperature = 0
                                        cache_prompt = $false; chat_template_kwargs = @{ enable_thinking = $false } }
    [ordered]@{ wall_s = [math]::Round($sw.Elapsed.TotalSeconds, 2); prompt_n = $r.timings.prompt_n
                prompt_s = [math]::Round($r.timings.prompt_ms / 1000, 2); prompt_tps = [math]::Round($r.timings.prompt_per_second, 1) }
}
function AvailableGB { [math]::Round((Get-Counter '\Memory\Available Bytes').CounterSamples[0].CookedValue / 1GB, 2) }

$serverArgs = @('-m', 'C:/AI/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf', '--host', '127.0.0.1', '--port', "$Port",
    '--ctx-size', '65536', '--fit', 'on', '--flash-attn', 'on', '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0',
    '--parallel', '1', '--jinja', '--metrics') + $Extra
$res = [ordered]@{ at = (Get-Date).ToString('s'); label = $Label; extra = ($Extra -join ' '); available_before_gb = AvailableGB }
$proc = Start-Process $exe -ArgumentList $serverArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardError (Join-Path $env:TEMP "llama-server-bench-$Label.log")
$watchdog = Start-Job -ArgumentList $proc.Id, $FloorGB -ScriptBlock {
    param($procId, $floor)
    $c = New-Object System.Diagnostics.PerformanceCounter('Memory', 'Available Bytes')
    $min = [double]::MaxValue
    while (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        $gb = $c.NextValue() / 1GB
        if ($gb -lt $min) { $min = $gb }
        if ($gb -lt $floor) { Stop-Process -Id $procId -Force; "killed at $([math]::Round($gb, 2)) GB" }
        Start-Sleep -Milliseconds 500
    }
    "min $([math]::Round($min, 2))"
}
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        if ($proc.HasExited) { throw 'llama-server exited (or the RAM watchdog stopped it)' }
        try { if ((Invoke-WebRequest "$base/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { break } } catch { }
        if ($sw.Elapsed.TotalSeconds -gt 400) { throw 'no /health within 400 s' }
        Start-Sleep -Milliseconds 250
    }
    $res.load_s = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $res.loaded = (& $measure -Label "$Label-loaded" -BaseUrl $base) | ConvertFrom-Json
    # Exactly $Tokens tokens of fixed text. /tokenize and /detokenize touch no weights, so the chat below is still
    # the first request that does.
    $src = (Get-ChildItem $docs -Filter *.md | Sort-Object Name | ForEach-Object { [IO.File]::ReadAllText($_.FullName) }) -join "`n"
    $tokens = (Post '/tokenize' @{ content = $src }).tokens
    $text = (Post '/detokenize' @{ tokens = @($tokens[0..($Tokens - 1)]) }).content
    $res.first = Chat $text
    $res.second = Chat $text
} catch {
    $res.error = "$_"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    $res.watchdog = (Receive-Job $watchdog -Wait -AutoRemoveJob) -join '; '
}
$line = $res | ConvertTo-Json -Depth 8 -Compress
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
Add-Content $Out $line
$line
