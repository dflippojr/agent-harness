<#
.SYNOPSIS
Deploys one tested main-branch commit from GitHub Actions to the tower.

.DESCRIPTION
This is intentionally fail-closed. It only updates a clean `main` checkout, requires the requested commit to be
the current origin/main, pulls immutable SHA-tagged sandbox images, and resolves dependencies in a side-by-side
virtual environment before downtime. It then stops the daemon, swaps the virtual environment, fast-forwards,
retags the images, and starts and health-checks the daemon. A failure after stop restores the old checkout and
untouched virtual environment before restarting the old daemon. It never runs for pull requests.
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [string]$ReleaseRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$DeployDir = 'D:\Projects\agent-harness',
    [string]$Registry = 'ghcr.io/dflippojr',
    [switch]$DryRun,
    [ValidateSet('None', 'AfterStop', 'AfterSwap', 'AfterMerge', 'AfterStart')]
    [string]$DryRunFailure = 'None'
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

function Get-DockerImageId([string]$Image) {
    if ($DryRun) { return "dry-run-$($Image.Split(':')[0].Split('/')[-1])-image-id" }
    $output = & docker image inspect --format '{{.Id}}' $Image
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "could not inspect existing image $Image (docker exit code $exitCode)" }
    if ($null -eq $output) { throw "could not inspect existing image ${Image}: docker returned no image id" }
    return ([string]::Join("`n", [string[]]$output)).Trim()
}

function Remove-DeploymentDirectory([string]$Path) {
    if ($DryRun) { Write-Host "[dry run] Remove-Item -Recurse $Path"; return }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

function Set-DeploymentVenvPointer([string]$Pointer, [string]$TemporaryPointer, [string]$Target) {
    if ($DryRun) { Write-Host "[dry run] Set-Content $Pointer -> $Target"; return }
    [System.IO.File]::WriteAllText($TemporaryPointer, $Target)
    if (Test-Path -LiteralPath $Pointer) {
        Remove-Item -LiteralPath $Pointer -Force
    }
    Move-Item -LiteralPath $TemporaryPointer -Destination $Pointer
}

function Restore-DeploymentVenvPointer(
    [string]$Pointer,
    [string]$TemporaryPointer,
    [bool]$PreviouslyExisted,
    [string]$PreviousTarget
) {
    if ($DryRun) {
        if ($PreviouslyExisted) { Write-Host "[dry run] Set-Content $Pointer -> $PreviousTarget" }
        else { Write-Host "[dry run] Remove-Item $Pointer" }
        return
    }
    if ($PreviouslyExisted) { Set-DeploymentVenvPointer $Pointer $TemporaryPointer $PreviousTarget }
    elseif (Test-Path -LiteralPath $Pointer) { Remove-Item -LiteralPath $Pointer -Force }
}

function Invoke-DryRunFailure([string]$Stage) {
    if ($DryRunFailure -eq $Stage) {
        $message = "simulated dry-run failure after $($Stage.Substring(5).ToLowerInvariant())"
        Write-Host "[dry run] $message"
        throw $message
    }
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
$previousCommit = Get-GitOutput -FailureMessage 'could not resolve live checkout HEAD' `
    -GitArguments @('-C', $deploy, 'rev-parse', 'HEAD')
if (-not $previousCommit) { throw 'could not resolve live checkout HEAD: git returned no commit' }

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
Write-Host '[deploy] guards passed'

$shaTag = "sha-$Commit"
$sandbox = "$Registry/agent-harness-sandbox:$shaTag"
$cli = "$Registry/agent-harness-cli:$shaTag"
$sandboxStable = 'agent-harness-sandbox:py312'
$cliStable = 'agent-harness-cli:1'
Run docker @('pull', $sandbox)
Run docker @('pull', $cli)
$previousSandboxImage = Get-DockerImageId $sandboxStable
$previousCliImage = Get-DockerImageId $cliStable

$liveVenv = Join-Path $deploy '.venv'
$venvPointer = Join-Path $deploy '.venv-path'
$deployParent = Split-Path $deploy -Parent
$deployName = Split-Path $deploy -Leaf
$venvPointerTemp = Join-Path $deployParent ".$deployName.venv-path-$Commit.tmp"
$hadVenvPointer = Test-Path -LiteralPath $venvPointer
if ($hadVenvPointer) {
    $pointerValue = Get-Content -Raw -LiteralPath $venvPointer
    if ($null -eq $pointerValue) { throw "deployment virtual environment pointer is empty: $venvPointer" }
    $previousVenv = ([string]$pointerValue).Trim()
    if (-not [System.IO.Path]::IsPathRooted($previousVenv) -or -not (Test-Path -LiteralPath $previousVenv)) {
        throw "deployment virtual environment pointer is invalid: $venvPointer"
    }
} else {
    $previousVenv = $liveVenv
}
$python = Join-Path $previousVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "deployment Python is missing: $python" }
$stagedVenv = Join-Path $deployParent ".$deployName.venv.deploy-$Commit"
if (Test-Path -LiteralPath $stagedVenv) { throw "staged virtual environment already exists: $stagedVenv" }
if (Test-Path -LiteralPath $venvPointerTemp) { throw "temporary virtual environment pointer already exists: $venvPointerTemp" }
$requirements = Join-Path $release 'requirements.txt'
if (-not $DryRun -and -not (Test-Path -LiteralPath $requirements)) {
    throw "release requirements are missing: $requirements"
}

try {
    Run $python @('-m', 'venv', $stagedVenv)
    $stagedPython = Join-Path $stagedVenv 'Scripts\python.exe'
    if (-not $DryRun -and -not (Test-Path -LiteralPath $stagedPython)) {
        throw "staged deployment Python is missing: $stagedPython"
    }
    Run $stagedPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements)
} catch {
    Remove-DeploymentDirectory $stagedVenv
    throw
}
Write-Host '[deploy] dependencies staged'

$restart = Join-Path $release 'ops\harness\restart-daemon.ps1'
if (-not $DryRun) {
    try { . $restart }
    catch {
        Remove-DeploymentDirectory $stagedVenv
        throw
    }
}
function Stop-DeploymentDaemon {
    if ($DryRun) {
        Write-Host "[dry run] powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restart -Mode Stop"
        return
    }
    Stop-HarnessDaemon
}
function Start-DeploymentDaemon {
    if ($DryRun) {
        Write-Host "[dry run] powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restart -Mode Start"
        return
    }
    Start-HarnessDaemon
}
$stopInitiated = $false
$venvSwitchInitiated = $false
$mergeInitiated = $false
$imageRetagInitiated = $false
try {
    $stopInitiated = $true
    Stop-DeploymentDaemon
    Write-Host '[deploy] daemon stopped'
    Invoke-DryRunFailure 'AfterStop'

    $venvSwitchInitiated = $true
    Set-DeploymentVenvPointer $venvPointer $venvPointerTemp $stagedVenv
    Write-Host '[deploy] virtual environment swapped'
    Invoke-DryRunFailure 'AfterSwap'

    $mergeInitiated = $true
    Run git @('-C', $deploy, 'merge', '--ff-only', $Commit)
    Write-Host '[deploy] checkout fast-forwarded'
    Invoke-DryRunFailure 'AfterMerge'

    $imageRetagInitiated = $true
    Run docker @('tag', $sandbox, $sandboxStable)
    Run docker @('tag', $cli, $cliStable)
    Start-DeploymentDaemon
    Write-Host '[deploy] daemon started and healthy'
    Invoke-DryRunFailure 'AfterStart'
} catch {
    $deploymentError = $_.Exception.Message
    if (-not $stopInitiated) {
        Remove-DeploymentDirectory $stagedVenv
        throw
    }

    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    try {
        Stop-DeploymentDaemon
        Write-Host '[rollback] stopped partially deployed daemon'
    } catch {
        $rollbackErrors.Add("daemon stop: $($_.Exception.Message)")
    }

    if ($mergeInitiated) {
        try {
            Run git @('-C', $deploy, 'reset', '--hard', $previousCommit)
            Write-Host "[rollback] restored checkout $previousCommit"
        } catch {
            $rollbackErrors.Add("checkout: $($_.Exception.Message)")
        }
    }

    try {
        if ($venvSwitchInitiated) {
            Restore-DeploymentVenvPointer $venvPointer $venvPointerTemp $hadVenvPointer $previousVenv
        }
        Write-Host '[rollback] restored previous virtual environment'
    } catch {
        $rollbackErrors.Add("virtual environment: $($_.Exception.Message)")
    }

    if ($imageRetagInitiated) {
        $imageRestoreFailed = $false
        try {
            Run docker @('tag', $previousSandboxImage, $sandboxStable)
            Write-Host '[rollback] restored previous sandbox image tag'
        } catch {
            $imageRestoreFailed = $true
            $rollbackErrors.Add("sandbox image tag: $($_.Exception.Message)")
        }
        try {
            Run docker @('tag', $previousCliImage, $cliStable)
            Write-Host '[rollback] restored previous CLI image tag'
        } catch {
            $imageRestoreFailed = $true
            $rollbackErrors.Add("CLI image tag: $($_.Exception.Message)")
        }
        if (-not $imageRestoreFailed) { Write-Host '[rollback] restored previous image tags' }
    }

    try {
        Start-DeploymentDaemon
        Write-Host '[rollback] previous daemon started and healthy'
    } catch { $rollbackErrors.Add("daemon restart: $($_.Exception.Message)") }

    try {
        $activeVenv = if (Test-Path -LiteralPath $venvPointer) {
            ([string](Get-Content -Raw -LiteralPath $venvPointer)).Trim()
        } else { $liveVenv }
        if ($activeVenv -eq $stagedVenv) {
            $rollbackErrors.Add('staged environment cleanup: skipped because it is still the active virtual environment')
        } else {
            Remove-DeploymentDirectory $stagedVenv
        }
    } catch { $rollbackErrors.Add("staged environment cleanup: $($_.Exception.Message)") }

    if ($rollbackErrors.Count -gt 0) {
        throw "deployment failed after daemon stop ($deploymentError); rollback also failed: $($rollbackErrors -join '; ')"
    }
    throw "deployment failed after daemon stop; previous deployment restored: $deploymentError"
}

$managedVenvPrefix = Join-Path $deployParent ".$deployName.venv.deploy-"
if ($hadVenvPointer -and $previousVenv.StartsWith($managedVenvPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    try { Remove-DeploymentDirectory $previousVenv }
    catch { Write-Warning "deployment succeeded but old virtual environment cleanup failed: $($_.Exception.Message)" }
}

Write-Host "Deployed $Commit"
Write-Host "  sandbox: $sandbox -> $sandboxStable"
Write-Host "  cli:     $cli -> $cliStable"
