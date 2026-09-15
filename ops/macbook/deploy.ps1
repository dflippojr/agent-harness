# Deploys the agent-harness runner to the MacBook over SSH and (re)starts its launchd agent.
#
#   ops\macbook\deploy.ps1 -MacHost <tailnet name or IP> -MacUser <user>
#
# Needs SSH access to the Mac (System Settings > General > Sharing > Remote Login) with the key in -KeyFile.
# The runner itself doesn't need SSH: it connects out to the daemon. Remote Login can be turned off afterwards;
# redeploying needs it again.
#
# Creates the runner token on first use (the daemon reads it from -TokenFile, see runners: in harness.yaml) and
# writes the Mac's runner/config.json with the daemon's public_url. Re-running updates the code and keeps the token.
param(
    [Parameter(Mandatory = $true)][string]$MacHost,
    [Parameter(Mandatory = $true)][string]$MacUser,
    [string]$KeyFile = "$HOME\.ssh\id_ed25519_macbook",
    [string]$TokenFile = 'D:\Agents\harness\secrets\runner-macbook.token',
    [string]$RunnerName = 'macbook',
    [string[]]$RepoRoots = @('~/Projects'),
    [double]$MinFreeGb = 10
)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$python = Join-Path $repo '.venv\Scripts\python.exe'
$sshArgs = @('-i', $KeyFile, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
$target = "$MacUser@$MacHost"

if (-not (Test-Path $TokenFile)) {
    New-Item -ItemType Directory -Force (Split-Path $TokenFile) | Out-Null
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [System.IO.File]::WriteAllText($TokenFile, $token)
    Write-Host "created runner token $TokenFile"
}
$token = (Get-Content $TokenFile -Raw).Trim()

Push-Location $repo
try { $server = & $python -c "from harness import config; print(config.load().public_url)" } finally { Pop-Location }
if (-not $server) { throw 'public_url is empty (config/harness.local.yaml); the runner needs it to reach the daemon' }

# Stage: runner, sandbox profile, the daemon modules it shares, launchd plist, install script, config. LF endings.
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("harness-mac-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force "$stage\app\harness" | Out-Null
$files = @{
    'macrunner\harness_runner.py'              = 'app\harness_runner.py'
    'macrunner\sandbox.sb'                     = 'app\sandbox.sb'
    'harness\fileops.py'                       = 'app\harness\fileops.py'
    'harness\projects.py'                      = 'app\harness\projects.py'
    'harness\changes.py'                       = 'app\harness\changes.py'
    'macrunner\install.sh'                     = 'install.sh'
    'macrunner\dev.agent-harness.runner.plist' = 'dev.agent-harness.runner.plist'
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
foreach ($src in $files.Keys) {
    $text = [System.IO.File]::ReadAllText((Join-Path $repo $src)) -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText((Join-Path $stage $files[$src]), $text, $utf8)
}
$config = [ordered]@{ server = $server; name = $RunnerName; token = $token; repo_roots = $RepoRoots; min_free_gb = $MinFreeGb }
[System.IO.File]::WriteAllText("$stage\config.json", ($config | ConvertTo-Json), $utf8)

try {
    $remoteDir = "/tmp/harness-deploy-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    & ssh @sshArgs $target "mkdir -m 700 $remoteDir"
    if ($LASTEXITCODE) { throw "ssh to $target failed" }
    & scp @sshArgs -q -r "$stage\*" "${target}:$remoteDir/"
    if ($LASTEXITCODE) { throw 'scp failed' }
    & ssh @sshArgs $target "bash $remoteDir/install.sh; rc=`$?; rm -rf $remoteDir; exit `$rc"
    if ($LASTEXITCODE) { throw 'install.sh failed on the Mac' }
} finally {
    Remove-Item -Recurse -Force $stage
}

# The daemon marks the runner online on its first poll.
foreach ($i in 1..15) {
    Start-Sleep -Seconds 2
    try {
        $r = (Invoke-RestMethod http://127.0.0.1:8100/runners -TimeoutSec 5) | Where-Object { $_.name -eq $RunnerName }
        if ($r.online) { Write-Host "runner $RunnerName online: $($r.info | ConvertTo-Json -Compress)"; exit 0 }
    } catch { }
}
Write-Warning "runner $RunnerName hasn't reached the daemon yet; check ~/.agent-harness/logs/runner.log on the Mac and that the daemon knows the runner (runners: in harness.yaml)"
