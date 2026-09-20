<#
.SYNOPSIS
Resets the staging smoke slot: stop it, then delete the variable state the candidate created (issue #129).

.DESCRIPTION
Clean, not uninstall. Deleted: everything directly under D:\Agents\harness-staging except the preserved entries —
SQLite (and its WAL files), workspaces, transcripts, the managed-config overlay, artifacts, images, the recorded
SHA, and the staging owner token. Preserved: the owner's harness.local.yaml overlay, logs, and the staging virtual
environment. The staging checkout is untouched, and no Docker image is removed, so production's stable tags and the
immutable SHA tags stay exactly as they are. Nothing is seeded: the slot comes back with empty session state.

Deleting the database rotates staging tokens, because every staging token and cookie only exists there.

Run it directly to stop and clean the slot, or dot-source it for Reset-StagingData.
#>
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'staging-common.ps1')
. (Join-Path $PSScriptRoot 'restart-daemon-staging.ps1')

# Everything else directly under the staging data root is disposable candidate state.
$StagingPreservedEntries = @('harness.local.yaml', 'logs', 'venv')

function Reset-StagingData {
    param([switch]$DryRun)
    Assert-StagingTarget -Path $StagingDataRoot -Role 'data root'
    if (-not (Test-Path -LiteralPath $StagingDataRoot)) {
        Write-Host "[staging reset] nothing to clean: $StagingDataRoot does not exist"
        return
    }
    foreach ($entry in Get-ChildItem -LiteralPath $StagingDataRoot -Force) {
        if ($StagingPreservedEntries -contains $entry.Name) {
            Write-Host "[staging reset] keeping $($entry.Name)"
            continue
        }
        Assert-StagingTarget -Path $entry.FullName -Role 'data entry'
        if ($DryRun) { Write-Host "[dry run] Remove-Item -Recurse $($entry.FullName)"; continue }
        Remove-Item -LiteralPath $entry.FullName -Recurse -Force
        Write-Host "[staging reset] deleted $($entry.Name)"
    }
    Write-Host '[staging reset] staging tokens rotated: the staging database and owner token file are gone'
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($DryRun) { Write-Host '[dry run] Stop-StagingDaemon' } else { Stop-StagingDaemon }
    Reset-StagingData -DryRun:$DryRun
    Write-Host '[staging reset] slot is stopped and clean; dispatch a deploy to fill it again'
}
