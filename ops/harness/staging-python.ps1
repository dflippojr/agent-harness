# Only the trusted staging deployer calls this, after stopping the staging slot.
. (Join-Path $PSScriptRoot 'staging-common.ps1')

function Test-StagingPython([string]$Python) {
    if (-not (Test-Path -LiteralPath $Python)) { return $false }
    & $Python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'
    return $LASTEXITCODE -eq 0
}

function Initialize-StagingPython {
    param([string]$Venv, [string]$BootstrapPython = '', [switch]$DryRun)
    $venvPath = [System.IO.Path]::GetFullPath($Venv)
    Assert-StagingTarget -Path $venvPath -Role 'virtual environment'
    $python = Join-Path $venvPath 'Scripts\python.exe'
    if ($DryRun) {
        Write-Host "[dry run] ensure Python 3.12 virtual environment at $venvPath; rebuild if incompatible"
        return
    }
    if (Test-StagingPython $python) { return }

    # A supplied interpreter must also be 3.12; never clear the old venv for an incompatible bootstrap.
    if ($BootstrapPython) {
        if (-not (Test-StagingPython $BootstrapPython)) { throw 'staging bootstrap must be Python 3.12' }
        & $BootstrapPython -m venv --clear $venvPath
    } else {
        # uv selects/downloads 3.12 even when PATH's python is 3.10.
        & uv venv --python 3.12 --seed --clear $venvPath
    }
    if ($LASTEXITCODE -ne 0) { throw 'could not build the staging Python 3.12 virtual environment' }
    if (-not (Test-StagingPython $python)) { throw "staging Python must be 3.12: $python" }
}
