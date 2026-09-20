# Reports what the staging slot is running (issue #129): the dispatched ref, the immutable commit that was
# resolved at job start, and the tailnet URL to open. Reads the slot's own recorded SHA so the report describes
# the running slot rather than the dispatch inputs.
param(
    [string]$RefLabel = '',
    [string]$Commit = '',
    [string]$StagingDataDir = ''
)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'staging-common.ps1')
if ($StagingDataDir) { $StagingDataRoot = $StagingDataDir }

$shaFile = Join-Path $StagingDataRoot 'deployed-sha.txt'
$running = if (Test-Path -LiteralPath $shaFile) { ([string](Get-Content -Raw -LiteralPath $shaFile)).Trim() } else { '' }

$lines = [System.Collections.Generic.List[string]]::new()
if ($running) {
    $lines.Add("Staging slot: $RefLabel")
    $lines.Add("Resolved commit: $running")
    $lines.Add("Open: https://<tower>.<tailnet>.ts.net:$StagingServePort/")
    $lines.Add("Confirm the SHA: (Invoke-RestMethod https://<tower>.<tailnet>.ts.net:$StagingServePort/health).build.commit")
    if ($Commit -and $Commit -ne $running) {
        $lines.Add("Note: the slot records $running, not the dispatched $Commit.")
    }
} else {
    $lines.Add('Staging slot: empty (reset). Dispatch a branch or same-repo pull request to fill it.')
}
$lines.Add("Production is unchanged: checkout D:\Projects\agent-harness on 127.0.0.1:$ProductionPort behind :443.")

$lines | ForEach-Object { Write-Host $_ }
if ($env:GITHUB_STEP_SUMMARY) {
    ($lines -join "`n") | Add-Content -Path $env:GITHUB_STEP_SUMMARY -Encoding utf8
}
