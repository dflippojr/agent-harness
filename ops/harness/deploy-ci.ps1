<#
.SYNOPSIS
Deploys one tested main-branch commit from GitHub Actions to the tower.

.DESCRIPTION
This is intentionally fail-closed. It only updates a clean `main` checkout, requires the requested commit to be
the current origin/main, pulls immutable SHA-tagged sandbox images, and resolves dependencies in a side-by-side
virtual environment before downtime. It then stops the daemon, swaps the virtual environment, fast-forwards,
retags the images, and starts and health-checks the daemon. A failure after stop restores the old checkout and
untouched virtual environment before restarting the old daemon. It never runs for pull requests.

Stables, the browser client, ships from dflippojr/stables (#334). The workflow checks out the tag pinned in
tools/stables/pin.json; this script verifies that checkout is the pinned commit, copies its public/ side by side
before downtime, and points .stables-path at the copy with the virtual environment swap. run-daemon.ps1 hands that
path to the daemon as HARNESS_STABLES_DIR. A rollback restores the previous pointer.

Under pwsh on Linux (#812) the daemon is the systemd unit install/install.sh's layout runs: -DaemonUnit runs
<InstallDir>/bin/run-daemon.sh for the live checkout, and <InstallDir>/venv is its environment until a deployment writes
.venv-path. The script stops and starts that unit with `sudo -n systemctl` and checks both before downtime. The
supervisor reads the pointers, so while the daemon is stopped it is replaced by the release's install/run-daemon.sh,
and a rollback puts the previous one back.
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [string]$ReleaseRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$DeployDir = $(if ($IsLinux) { '/home/daniel/Projects/agent-harness' } else { 'D:\Projects\agent-harness' }),
    # Linux only: the install/install.sh layout and the systemd unit that runs the daemon.
    [string]$InstallDir = '/home/daniel/.local/share/agent-harness',
    [string]$DaemonUnit = 'agent-harness-main-daemon.service',
    [string]$Registry = 'ghcr.io/dflippojr',
    [string]$StablesRelease = '',
    [switch]$DryRun,
    [ValidateSet('None', 'AfterStop', 'AfterSwap', 'AfterMerge', 'AfterStart')]
    [string]$DryRunFailure = 'None'
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 has no $IsLinux.
$onLinux = [bool]$IsLinux
$venvPython = if ($onLinux) { 'bin/python' } else { 'Scripts\python.exe' }

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

function Set-DeploymentPointer([string]$Pointer, [string]$TemporaryPointer, [string]$Target) {
    if ($DryRun) { Write-Host "[dry run] Set-Content $Pointer -> $Target"; return }
    [System.IO.File]::WriteAllText($TemporaryPointer, $Target)
    if (Test-Path -LiteralPath $Pointer) {
        Remove-Item -LiteralPath $Pointer -Force
    }
    Move-Item -LiteralPath $TemporaryPointer -Destination $Pointer
}

function Restore-DeploymentPointer(
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
    if ($PreviouslyExisted) { Set-DeploymentPointer $Pointer $TemporaryPointer $PreviousTarget }
    elseif (Test-Path -LiteralPath $Pointer) { Remove-Item -LiteralPath $Pointer -Force }
}

function Copy-DeploymentStables([string]$Source, [string]$Target) {
    if ($DryRun) { Write-Host "[dry run] Copy-Item -Recurse $Source -> $Target"; return }
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Target -Recurse
}

function Set-DeploymentSupervisor([string]$Path, [string]$Text, [string]$Source) {
    if ($DryRun) { Write-Host "[dry run] install $Source -> $Path"; return }
    # Rewriting the file in place keeps its mode; the unit reads it only when it starts.
    [System.IO.File]::WriteAllText($Path, $Text)
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
if ($onLinux) {
    $supervisor = [System.IO.Path]::Combine($InstallDir, 'bin', 'run-daemon.sh')
    # A unit that runs another supervisor or checkout would never read the pointers this deployment switches.
    $execStart = & systemctl show --property=ExecStart --value $DaemonUnit
    if ($LASTEXITCODE -ne 0) { throw "could not read $DaemonUnit (systemctl exit code $LASTEXITCODE)" }
    $execStart = [string]::Join(' ', [string[]]@($execStart))
    if (-not ($execStart.Contains(" $supervisor ") -and $execStart.Contains(" $deploy "))) {
        throw "$DaemonUnit does not run $supervisor for $deploy; refusing deployment. ExecStart: $execStart"
    }
    if (-not (Test-Path -LiteralPath $supervisor)) { throw "daemon supervisor is missing: $supervisor" }
    foreach ($action in @('stop', 'start')) {
        & sudo -n -l systemctl $action $DaemonUnit | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "this user may not run sudo -n systemctl $action $DaemonUnit; refusing deployment before downtime"
        }
    }
    Write-Host "[deploy] $DaemonUnit runs $supervisor"
}

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

$pinPath = [System.IO.Path]::Combine($release, 'tools', 'stables', 'pin.json')
if (-not (Test-Path -LiteralPath $pinPath)) { throw "Stables pin is missing: $pinPath" }
$stablesPin = Get-Content -Raw -LiteralPath $pinPath | ConvertFrom-Json
if ([string]$stablesPin.commit -notmatch '^[0-9a-f]{40}$') { throw "Stables pin names no commit: $pinPath" }
if (-not $StablesRelease) { $StablesRelease = Join-Path $release 'stables-release' }
$stablesIndex = [System.IO.Path]::Combine($StablesRelease, 'public', 'index.html')
if (-not (Test-Path -LiteralPath $stablesIndex)) { throw "Stables checkout has no $stablesIndex" }
$stablesCommit = Get-GitOutput -FailureMessage 'could not resolve the Stables checkout HEAD' `
    -GitArguments @('-C', $StablesRelease, 'rev-parse', 'HEAD')
if ($stablesCommit -ne $stablesPin.commit) {
    throw "Stables checkout is $stablesCommit, but tools/stables/pin.json pins $($stablesPin.tag) at $($stablesPin.commit)"
}
Write-Host "[deploy] Stables $($stablesPin.tag) verified"

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
} elseif ($onLinux) {
    $previousVenv = Join-Path $InstallDir 'venv'
} else {
    $previousVenv = $liveVenv
}
$python = Join-Path $previousVenv $venvPython
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
    $stagedPython = Join-Path $stagedVenv $venvPython
    if (-not $DryRun -and -not (Test-Path -LiteralPath $stagedPython)) {
        throw "staged deployment Python is missing: $stagedPython"
    }
    Run $stagedPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements)
    # Optional tracing packages (#326); repomap stays opt-in.
    $telemetryRequirements = Join-Path $release 'requirements-telemetry.txt'
    if ($DryRun -or (Test-Path -LiteralPath $telemetryRequirements)) {
        Run $stagedPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $telemetryRequirements)
    }
} catch {
    Remove-DeploymentDirectory $stagedVenv
    throw
}
Write-Host '[deploy] dependencies staged'

$stablesPointer = Join-Path $deploy '.stables-path'
$stablesPointerTemp = Join-Path $deployParent ".$deployName.stables-path-$Commit.tmp"
$hadStablesPointer = Test-Path -LiteralPath $stablesPointer
$previousStables = if ($hadStablesPointer) { ([string](Get-Content -Raw -LiteralPath $stablesPointer)).Trim() } else { '' }
$managedStablesPrefix = Join-Path $deployParent ".$deployName.stables-"
$stagedStablesRoot = "$managedStablesPrefix$stablesCommit"
$stagedStables = Join-Path $stagedStablesRoot 'public'
$stablesChanged = ($previousStables -ne $stagedStables) -or `
    -not (Test-Path -LiteralPath (Join-Path $stagedStables 'index.html'))
if ($stablesChanged) {
    try {
        # A copy left by an interrupted deployment is never the active one here, so it is replaced.
        Remove-DeploymentDirectory $stagedStablesRoot
        Copy-DeploymentStables (Join-Path $StablesRelease 'public') $stagedStablesRoot
    } catch {
        Remove-DeploymentDirectory $stagedStablesRoot
        Remove-DeploymentDirectory $stagedVenv
        throw
    }
}
Write-Host '[deploy] Stables staged'

if ($onLinux) {
    $releaseSupervisor = [System.IO.Path]::Combine($release, 'install', 'run-daemon.sh')
    if (-not (Test-Path -LiteralPath $releaseSupervisor)) { throw "release supervisor is missing: $releaseSupervisor" }
    $supervisorText = [System.IO.File]::ReadAllText($releaseSupervisor)
    $previousSupervisorText = [System.IO.File]::ReadAllText($supervisor)
    $supervisorChanged = $supervisorText -cne $previousSupervisorText
}

$restart = Join-Path $release 'ops\harness\restart-daemon.ps1'
if (-not $DryRun -and -not $onLinux) {
    try { . $restart }
    catch {
        Remove-DeploymentDirectory $stagedVenv
        if ($stablesChanged) { Remove-DeploymentDirectory $stagedStablesRoot }
        throw
    }
}
function Wait-LinuxDaemonHealth {
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        try {
            if ((Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 3).ok) {
                Write-Host "daemon up after $($i * 2) s"
                return
            }
        } catch { }
    }
    throw "daemon did not come back within 60 s; see journalctl -u $DaemonUnit and $InstallDir/logs"
}
function Stop-DeploymentDaemon {
    if ($onLinux) { Run sudo @('-n', 'systemctl', 'stop', $DaemonUnit); return }
    if ($DryRun) {
        Write-Host "[dry run] powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restart -Mode Stop"
        return
    }
    Stop-HarnessDaemon
}
function Start-DeploymentDaemon {
    if ($onLinux) {
        Run sudo @('-n', 'systemctl', 'start', $DaemonUnit)
        if (-not $DryRun) { Wait-LinuxDaemonHealth }
        return
    }
    if ($DryRun) {
        Write-Host "[dry run] powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restart -Mode Start"
        return
    }
    Start-HarnessDaemon
}
$stopInitiated = $false
$venvSwitchInitiated = $false
$stablesSwitchInitiated = $false
$supervisorSwitchInitiated = $false
$mergeInitiated = $false
$imageRetagInitiated = $false
try {
    $stopInitiated = $true
    Stop-DeploymentDaemon
    Write-Host '[deploy] daemon stopped'
    Invoke-DryRunFailure 'AfterStop'

    $venvSwitchInitiated = $true
    Set-DeploymentPointer $venvPointer $venvPointerTemp $stagedVenv
    Write-Host '[deploy] virtual environment swapped'
    if ($stablesChanged) {
        $stablesSwitchInitiated = $true
        Set-DeploymentPointer $stablesPointer $stablesPointerTemp $stagedStables
    }
    Write-Host "[deploy] Stables $($stablesPin.tag) selected"
    if ($onLinux -and $supervisorChanged) {
        $supervisorSwitchInitiated = $true
        Set-DeploymentSupervisor $supervisor $supervisorText $releaseSupervisor
        Write-Host '[deploy] supervisor updated'
    }
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
        if ($stablesChanged) { Remove-DeploymentDirectory $stagedStablesRoot }
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
            Restore-DeploymentPointer $venvPointer $venvPointerTemp $hadVenvPointer $previousVenv
        }
        Write-Host '[rollback] restored previous virtual environment'
    } catch {
        $rollbackErrors.Add("virtual environment: $($_.Exception.Message)")
    }

    try {
        if ($stablesSwitchInitiated) {
            Restore-DeploymentPointer $stablesPointer $stablesPointerTemp $hadStablesPointer $previousStables
        }
        Write-Host '[rollback] restored previous Stables'
    } catch {
        $rollbackErrors.Add("Stables: $($_.Exception.Message)")
    }

    if ($supervisorSwitchInitiated) {
        try {
            Set-DeploymentSupervisor $supervisor $previousSupervisorText 'the previous supervisor'
            Write-Host '[rollback] restored previous supervisor'
        } catch {
            $rollbackErrors.Add("supervisor: $($_.Exception.Message)")
        }
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

    try {
        $activeStables = if (Test-Path -LiteralPath $stablesPointer) {
            ([string](Get-Content -Raw -LiteralPath $stablesPointer)).Trim()
        } else { '' }
        if ($stablesChanged -and $activeStables -ne $stagedStables) { Remove-DeploymentDirectory $stagedStablesRoot }
    } catch { $rollbackErrors.Add("staged Stables cleanup: $($_.Exception.Message)") }

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

if ($stablesChanged -and $previousStables.StartsWith($managedStablesPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        $previousStables -ne $stagedStables) {
    try { Remove-DeploymentDirectory (Split-Path $previousStables -Parent) }
    catch { Write-Warning "deployment succeeded but old Stables cleanup failed: $($_.Exception.Message)" }
}

Write-Host "Deployed $Commit"
Write-Host "  sandbox: $sandbox -> $sandboxStable"
Write-Host "  cli:     $cli -> $cliStable"
Write-Host "  stables: $($stablesPin.tag) ($stablesCommit) -> $stagedStables"
