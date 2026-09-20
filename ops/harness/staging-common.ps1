<#
.SYNOPSIS
Literal staging locations and the production guards every staging script must pass (issue #129).

.DESCRIPTION
Dot-source this from the staging scripts. The paths below are written out in full on purpose: no staging location
is ever derived from a production location at runtime, so a typo cannot resolve to the production checkout, data
root, task, or port. Every staging entry point calls Assert-StagingTarget before it creates, deletes, or starts
anything.
#>

$StagingCheckout = 'D:\Projects\agent-harness-staging'
$StagingDataRoot = 'D:\Agents\harness-staging'
$StagingVenv = 'D:\Agents\harness-staging\venv'
$StagingLogDir = 'D:\Agents\harness-staging\logs'
$StagingLocalConfig = 'D:\Agents\harness-staging\harness.local.yaml'
$StagingTaskName = 'AgentHarness-Daemon-Staging'
$StagingPort = 8101
$StagingServePort = 8444
$StagingRepository = 'https://github.com/dflippojr/agent-harness.git'
# Staging-only local tag. Production reads agent-harness-sandbox:py312 and agent-harness-cli:1; staging must never
# create, retag, or delete those.
$StagingSandboxImage = 'agent-harness-sandbox:staging'

$ProductionCheckout = 'D:\Projects\agent-harness'
$ProductionDataRoot = 'D:\Agents\harness'
$ProductionTaskName = 'AgentHarness-Daemon'
$ProductionPort = 8100

function Split-PathSegments([string]$Path) {
    $normalized = $Path.Replace('/', '\').TrimEnd('\')
    return , @($normalized.Split('\') | Where-Object { $_ -ne '' })
}

function Test-PathInside([string]$Path, [string]$Container) {
    # Segment comparison, so D:\Agents\harness-staging is not treated as living inside D:\Agents\harness.
    $pathSegments = Split-PathSegments $Path
    $containerSegments = Split-PathSegments $Container
    if ($pathSegments.Count -lt $containerSegments.Count) { return $false }
    for ($i = 0; $i -lt $containerSegments.Count; $i++) {
        if ($pathSegments[$i] -ne $containerSegments[$i]) { return $false }
    }
    return $true
}

function Assert-StagingTarget {
    <#
    .SYNOPSIS
    Throws unless $Path is an absolute path that is neither a production location nor inside one.
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Role)
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw "staging $Role must be an absolute path: $Path" }
    foreach ($production in @($ProductionCheckout, $ProductionDataRoot)) {
        if (Test-PathInside $Path $production) {
            throw "staging $Role would write inside the production location ${production}: $Path"
        }
    }
}

function Assert-StagingTask([Parameter(Mandatory)][string]$TaskName) {
    if ($TaskName -eq $ProductionTaskName) { throw "refusing to manage the production task $ProductionTaskName" }
    if ($TaskName -ne $StagingTaskName) { throw "unexpected staging task name: $TaskName" }
}

function Assert-StagingPort([Parameter(Mandatory)][int]$Port) {
    if ($Port -eq $ProductionPort) { throw "refusing to use the production port $ProductionPort for staging" }
    if ($Port -ne $StagingPort) { throw "unexpected staging port: $Port" }
}
