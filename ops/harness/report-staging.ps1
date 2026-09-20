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

# The tailnet name is only known on the tower, from the owner's staging overlay.
$url = "https://<tower>.<tailnet>.ts.net:$StagingServePort"
$localConfig = Join-Path $StagingDataRoot 'harness.local.yaml'
if (Test-Path -LiteralPath $localConfig) {
    $match = [regex]::Match((Get-Content -Raw -LiteralPath $localConfig), '(?m)^public_url:\s*"?([^"\s]+)"?\s*$')
    if ($match.Success) { $url = $match.Groups[1].Value.TrimEnd('/') }
}

$lines = [System.Collections.Generic.List[string]]::new()
if ($running) {
    $lines.Add("Staging slot: $RefLabel")
    $lines.Add("Resolved commit: $running")
    $lines.Add("Open: $url/")
    $lines.Add("Confirm the SHA: (Invoke-RestMethod $url/health).build.commit")
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
