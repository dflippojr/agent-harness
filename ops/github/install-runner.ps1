<# Registers a pinned GitHub Actions runner as a hidden per-user logon task. The registration token is used once. #>
param(
    [Parameter(Mandatory)][string]$Token,
    [string]$Repo = 'dflippojr/agent-harness',
    [string]$InstallDir = 'D:\Agents\github-runner',
    [string]$WorkDir = '_work',
    [string]$Name = 'dflippotower-agent-harness',
    [string]$Version = '2.337.0',
    [string]$Sha256 = '1150692afa94e71f872017e254ea55b6eece1eece3fe7e3a6d4c93d0a1b85cfc'
)
$ErrorActionPreference = 'Stop'
$taskName = 'AgentHarness-GitHubRunner'
$zip = Join-Path $env:TEMP "actions-runner-win-x64-$Version.zip"
$url = "https://github.com/actions/runner/releases/download/v$Version/actions-runner-win-x64-$Version.zip"

if (Test-Path -LiteralPath (Join-Path $InstallDir '.runner')) {
    throw "a runner is already configured in $InstallDir"
}
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if (-not (Test-Path -LiteralPath $zip)) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip }
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
if ($actual -ne $Sha256) { throw "runner archive hash mismatch: $actual" }
Expand-Archive -LiteralPath $zip -DestinationPath $InstallDir -Force

Push-Location $InstallDir
try {
    & .\config.cmd --unattended --replace --url "https://github.com/$Repo" --token $Token --name $Name `
        --labels agent-harness-tower --work $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "runner registration failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }

$launchArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$InstallDir\run.cmd'`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $launchArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'GitHub Actions deployment runner for dflippojr/agent-harness main pushes.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
