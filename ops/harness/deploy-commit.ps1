# Derives the deployed commit from the deployment venv pointer value (".agent-harness.venv.deploy-<40-hex sha>").
# Returns '' for a dev checkout (no pointer, or a path that does not end in a full SHA) so nothing else can leak
# into HARNESS_BUILD_COMMIT.
function Get-DeployedCommit([string]$VenvPath) {
    if ([string]::IsNullOrWhiteSpace($VenvPath)) { return '' }
    $leaf = Split-Path $VenvPath.Trim().TrimEnd('\', '/') -Leaf
    if ($leaf -match '^\.agent-harness\.venv\.deploy-([0-9a-fA-F]{40})$') { return $Matches[1].ToLowerInvariant() }
    return ''
}
