<#
.SYNOPSIS
Deploys one tested main-branch commit from GitHub Actions to the tower.

.DESCRIPTION
This is intentionally fail-closed. It only updates a clean `main` checkout, requires the requested commit to be
the current origin/main, pulls immutable SHA-tagged sandbox images before touching the live checkout, installs
Python requirements from the Actions checkout, fast-forwards, retags the images to the daemon's stable local names,
and restarts the existing host daemon. It never runs for pull requests.
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [string]$ReleaseRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$DeployDir = 'D:\Projects\agent-harness',
    [string]$Registry = 'ghcr.io/dflippojr',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

function Run([string]$File, [string[]]$Arguments) {
    if ($DryRun) { Write-Host "[dry run] $File $($Arguments -join ' ')"; return }
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}

function Get-GitOutput {
    param(
        [Parameter(Mandatory)][string]$FailureMessage,
        [Parameter(Mandatory)][string[]]$GitArguments
    )
    $output = & git @GitArguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "$FailureMessage (git exit code $exitCode)" }
    if ($null -eq $output) { return [string]::Empty }
    return ([string]::Join("`n", [string[]]$output)).Trim()
}

$release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$deploy = (Resolve-Path -LiteralPath $DeployDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $release '.git'))) { throw "release checkout is not a git repository: $release" }
if (-not (Test-Path -LiteralPath (Join-Path $deploy '.git'))) { throw "deploy checkout is not a git repository: $deploy" }

$releaseCommit = Get-GitOutput -FailureMessage 'could not resolve Actions checkout HEAD' `
    -GitArguments @('-C', $release, 'rev-parse', 'HEAD')
if (-not $releaseCommit) { throw 'could not resolve Actions checkout HEAD: git returned no commit' }
if ($releaseCommit -ne $Commit) {
    throw "Actions checkout is $releaseCommit, expected $Commit"
}
$branch = Get-GitOutput -FailureMessage 'could not determine live checkout branch' `
    -GitArguments @('-C', $deploy, 'branch', '--show-current')
if (-not $branch) { throw 'live checkout must be on main; current branch is detached or unknown' }
if ($branch -ne 'main') { throw "live checkout must be on main, not $branch" }
$dirty = & git -C $deploy status --porcelain
if ($LASTEXITCODE -ne 0) { throw "could not inspect live checkout (git exit code $LASTEXITCODE)" }
if ($dirty) { throw "live checkout has uncommitted files; refusing deployment:`n$($dirty -join "`n")" }

Run git @('-C', $deploy, 'fetch', '--prune', 'origin', 'main')
if (-not $DryRun) {
    $remoteCommit = Get-GitOutput -FailureMessage 'could not resolve origin/main' `
        -GitArguments @('-C', $deploy, 'rev-parse', 'origin/main')
    if (-not $remoteCommit) { throw 'could not resolve origin/main: git returned no commit' }
    if ($remoteCommit -ne $Commit) {
        Write-Host "origin/main has moved to $remoteCommit; skipping obsolete deployment for $Commit"
        exit 0
    }
    & git -C $deploy merge-base --is-ancestor HEAD $Commit
    if ($LASTEXITCODE -ne 0) { throw "live checkout is not an ancestor of $Commit; refusing a non-fast-forward deployment" }
}

$shaTag = "sha-$Commit"
$sandbox = "$Registry/agent-harness-sandbox:$shaTag"
$cli = "$Registry/agent-harness-cli:$shaTag"
Run docker @('pull', $sandbox)
Run docker @('pull', $cli)

$python = Join-Path $deploy '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "deployment Python is missing: $python" }
Run $python @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $release 'requirements.txt'))
Run git @('-C', $deploy, 'merge', '--ff-only', $Commit)
Run docker @('tag', $sandbox, 'agent-harness-sandbox:py312')
Run docker @('tag', $cli, 'agent-harness-cli:1')

$restart = Join-Path $deploy 'ops\harness\restart-daemon.ps1'
if ($DryRun) { Write-Host "[dry run] powershell.exe -File $restart" }
else { & $restart; if ($LASTEXITCODE -ne 0) { throw "daemon restart failed with exit code $LASTEXITCODE" } }

Write-Host "Deployed $Commit"
Write-Host "  sandbox: $sandbox -> agent-harness-sandbox:py312"
Write-Host "  cli:     $cli -> agent-harness-cli:1"
