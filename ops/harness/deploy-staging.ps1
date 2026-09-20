<#
.SYNOPSIS
Deploys one resolved commit into the tower's staging smoke slot, or resets that slot (issue #129).

.DESCRIPTION
This is the trusted deployer: it always runs from the `main` checkout that GitHub Actions makes on the staging
runner, never from the candidate's own copy. The candidate commit is checked out into D:\Projects\agent-harness-staging
and run as the AgentHarness-Daemon-Staging task on 127.0.0.1:8101 against the D:\Agents\harness-staging data root.
It is a trusted-code smoke slot: isolation exists to prevent accidents, not to sandbox hostile code.

Nothing here reads or writes the production checkout, data root, scheduled task, port 8100, `:443` route, secrets,
or the stable `agent-harness-sandbox:py312` / `agent-harness-cli:1` tags. Every staging location is a literal path
guarded by Assert-StagingTarget, and a failure at any stage stops staging and leaves production exactly as it was.

A deploy always starts from a clean slot: staging is stopped, staging data (and therefore every staging token) is
deleted, then the resolved commit is started. The owner's harness.local.yaml overlay, the staging logs, the staging
checkout, and the staging virtual environment survive.
#>
param(
    [ValidatePattern('^([0-9a-f]{40})?$')][string]$Commit = '',
    [switch]$Reset,
    [string]$StagingDir = '',
    [string]$StagingDataDir = '',
    [string]$Repository = '',
    [string]$ReleaseRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$BootstrapPython = 'python',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

# Dot-sourcing a script runs its param block in this scope, so capture the switch before that can reset it.
$planOnly = [bool]$DryRun
. (Join-Path $PSScriptRoot 'staging-common.ps1')
. (Join-Path $PSScriptRoot 'reset-staging.ps1')

if ($StagingDir) { $StagingCheckout = $StagingDir }
if ($StagingDataDir) { $StagingDataRoot = $StagingDataDir }
if ($Repository) { $StagingRepository = $Repository }
$StagingVenv = Join-Path $StagingDataRoot 'venv'
$StagingLogDir = Join-Path $StagingDataRoot 'logs'
$StagingLocalConfig = Join-Path $StagingDataRoot 'harness.local.yaml'

function Run([string]$File, [string[]]$Arguments) {
    if ($planOnly) { Write-Host "[dry run] $File $($Arguments -join ' ')"; return }
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}

function Assert-NoProductionReference([string]$Path) {
    $text = Get-Content -Raw -LiteralPath $Path
    foreach ($pattern in @('D:/Agents/harness/', 'D:\Agents\harness\', 'D:/Projects/agent-harness/',
            'D:\Projects\agent-harness\', 'port: 8100')) {
        if ($text -like "*$pattern*") {
            throw "staging overlay $Path references production ('$pattern'); staging must not read production state"
        }
    }
    if ($text -like '*REPLACE_WITH_OWNER_TAILSCALE_LOGIN*') {
        throw "staging overlay $Path still has the placeholder login; set allowed_logins to the owner's Tailscale login"
    }
    if ($text -notmatch '(?m)^allowed_logins:\s*\[\s*[^\s\]]') {
        throw "staging overlay $Path must set allowed_logins to exactly the owner's Tailscale login"
    }
    if ($text -notmatch "(?m)^\s+port:\s*$StagingPort\s*$") {
        throw "staging overlay $Path must bind port $StagingPort"
    }
}

if ($Reset -and $Commit) { throw 'pass either -Commit or -Reset, not both' }
if (-not $Reset -and -not $Commit) { throw 'pass -Commit <40-hex sha> or -Reset' }

Assert-StagingTarget -Path $StagingCheckout -Role 'checkout'
Assert-StagingTarget -Path $StagingDataRoot -Role 'data root'
Assert-StagingTarget -Path $StagingVenv -Role 'virtual environment'
Assert-StagingTask $StagingTaskName
Assert-StagingPort $StagingPort
Write-Host '[staging] guards passed'

if ($planOnly) { Write-Host '[dry run] Stop-StagingDaemon' } else { Stop-StagingDaemon }
Write-Host '[staging] slot stopped'

Reset-StagingData -DryRun:$planOnly
Write-Host '[staging] slot clean'

if ($Reset) {
    Write-Host '[staging] reset complete; the slot is stopped, staging tokens are rotated, and nothing was seeded'
    Write-Host 'Production checkout, SHA, daemon, data, credentials, Docker tags, GPU, and :443 were not touched.'
    exit 0
}

try {
    New-Item -ItemType Directory -Force $StagingDataRoot | Out-Null
    New-Item -ItemType Directory -Force $StagingLogDir | Out-Null

    if (-not (Test-Path -LiteralPath (Join-Path $StagingCheckout '.git'))) {
        Run git @('clone', $StagingRepository, $StagingCheckout)
    }
    # Same-repo branches and same-repo pull-request heads only; the ref was already validated off-tower.
    Run git @('-C', $StagingCheckout, 'fetch', '--prune', '--force', 'origin',
        '+refs/heads/*:refs/remotes/origin/*', '+refs/pull/*/head:refs/remotes/origin/pr/*')
    if (-not $planOnly) {
        & git -C $StagingCheckout cat-file -e "$Commit^{commit}"
        if ($LASTEXITCODE -ne 0) { throw "$Commit is not a commit in this repository's fetched refs" }
    }
    Run git @('-C', $StagingCheckout, 'checkout', '--detach', '--force', $Commit)
    Run git @('-C', $StagingCheckout, 'reset', '--hard', $Commit)
    Run git @('-C', $StagingCheckout, 'clean', '-ffdx')
    Write-Host "[staging] checkout at $Commit"

    $stagingPython = Join-Path $StagingVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $stagingPython)) {
        Run $BootstrapPython @('-m', 'venv', $StagingVenv)
    }
    if (-not $planOnly -and -not (Test-Path -LiteralPath $stagingPython)) {
        throw "staging Python is missing: $stagingPython"
    }
    $requirements = Join-Path $StagingCheckout 'requirements.txt'
    if (-not $planOnly -and -not (Test-Path -LiteralPath $requirements)) {
        throw "candidate requirements are missing: $requirements"
    }
    Run $stagingPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements)
    Write-Host '[staging] dependencies installed'

    # Capability overlay: the trusted profile file wins over the candidate's harness.yaml and the owner's local
    # overlay for `profile` and `modules`, so the candidate cannot switch a forced-off module back on.
    $candidateConfig = Join-Path $StagingCheckout 'config'
    if (-not $planOnly -and -not (Test-Path -LiteralPath $candidateConfig)) {
        throw "candidate config directory is missing: $candidateConfig"
    }
    if (-not (Test-Path -LiteralPath $StagingLocalConfig)) {
        $template = Join-Path $ReleaseRoot 'ops\harness\staging-harness.local.yaml'
        if ($planOnly) { Write-Host "[dry run] Copy-Item $template -> $StagingLocalConfig" }
        else { Copy-Item -LiteralPath $template -Destination $StagingLocalConfig }
        Write-Host "[staging] created the owner overlay $StagingLocalConfig from the template"
    }
    if (Test-Path -LiteralPath $StagingLocalConfig) { Assert-NoProductionReference $StagingLocalConfig }
    if (-not $planOnly) {
        Copy-Item -LiteralPath (Join-Path $ReleaseRoot 'ops\harness\staging-profile.yaml') `
            -Destination (Join-Path $candidateConfig 'profile.yaml') -Force
        Copy-Item -LiteralPath $StagingLocalConfig -Destination (Join-Path $candidateConfig 'harness.local.yaml') -Force
        [System.IO.File]::WriteAllText((Join-Path $StagingDataRoot 'deployed-sha.txt'), $Commit)
    } else {
        Write-Host "[dry run] Copy-Item staging-profile.yaml -> $candidateConfig\profile.yaml"
        Write-Host "[dry run] Copy-Item $StagingLocalConfig -> $candidateConfig\harness.local.yaml"
        Write-Host "[dry run] Set-Content deployed-sha.txt -> $Commit"
    }
    Write-Host '[staging] overlay applied with every optional module forced off'

    if (-not $planOnly) {
        try { Get-ScheduledTask -TaskName $StagingTaskName -ErrorAction Stop | Out-Null }
        catch { throw "$StagingTaskName is not installed; run ops\harness\install-task-staging.ps1 on the tower once" }
    }
    if ($planOnly) { Write-Host '[dry run] Start-StagingDaemon' } else { Start-StagingDaemon }
    Write-Host '[staging] daemon started and healthy'

    if ($planOnly) {
        Write-Host "[dry run] verify http://127.0.0.1:$StagingPort/health capabilities"
        Write-Host '[dry run] mint staging owner token'
    } else {
        $health = Invoke-RestMethod "http://127.0.0.1:$StagingPort/health" -TimeoutSec 10
        $enabled = @($health.capabilities.modules.PSObject.Properties |
            Where-Object { $_.Value } | ForEach-Object { $_.Name })
        $hosted = @($health.capabilities.hosted_backends)
        if ($enabled.Count -gt 0 -or $hosted.Count -gt 0) {
            throw ("staging came up with more than the smoke profile (profile $($health.profile), " +
                "modules [$($enabled -join ', ')], hosted backends [$($hosted -join ', ')]); refusing to leave it up")
        }
        Write-Host '[staging] capabilities verified: smoke profile only, no hosted backends'

        $tokenFile = Join-Path $StagingDataRoot 'owner-token.txt'
        Run $stagingPython @((Join-Path $ReleaseRoot 'scripts\staging_owner_token.py'),
            '--data-dir', $StagingDataRoot, '--token-file', $tokenFile, '--harness-root', $StagingCheckout)
    }
    Write-Host "[staging] owner token bootstrapped; read it on the tower with: Get-Content $(Join-Path $StagingDataRoot 'owner-token.txt')"
} catch {
    $deployError = $_.Exception.Message
    try {
        if ($planOnly) { Write-Host '[dry run] Stop-StagingDaemon' } else { Stop-StagingDaemon }
        Write-Host '[staging] stopped the failed slot'
    } catch {
        Write-Warning "staging deploy failed and the slot could not be stopped: $($_.Exception.Message)"
    }
    Write-Host 'Production checkout, SHA, daemon, data, credentials, Docker tags, GPU, and :443 were not touched.'
    throw "staging deployment failed: $deployError"
}

Write-Host "Staged $Commit"
Write-Host "  checkout: $StagingCheckout"
Write-Host "  data:     $StagingDataRoot"
Write-Host "  bind:     127.0.0.1:$StagingPort (tailnet :$StagingServePort)"
