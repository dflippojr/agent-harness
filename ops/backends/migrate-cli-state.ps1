# One-time owner step for per-domain CLI state (#371). Run it in a terminal on this PC right after the daemon with
# per-domain volumes is deployed, while no Claude Code or Cursor session is running:
#
#   ops\backends\migrate-cli-state.ps1 -Check   # report what would move, change nothing
#   ops\backends\migrate-cli-state.ps1          # move the logins
#
# The existing volumes stay the Web domain's state, so the owner's sessions keep their history and --resume. Only the
# credentials move out of them, into the login volumes every domain shares:
#   Claude: harness-auth-claude/.credentials.json   -> harness-login-claude/.credentials.json
#   Cursor: harness-auth-cursor/home/.config/cursor/ -> harness-login-cursor/   (auth.json)
#   Codex:  nothing; harness-auth-codex stays the owner's state and login. Apps log in on their own
#           (ops\backends\login.ps1 codex -App <app id>).
# It is safe to run again: a login that already moved is reported and left alone. The daemon never runs this.
param(
    [switch]$Check,
    [string]$Image = 'agent-harness-cli:1',
    [string]$Prefix = 'harness'  # volume name prefix; only tests change it
)
$ErrorActionPreference = 'Stop'

# Runs as root in a throwaway container with the old volume at /old and the login volume at /new. No double quotes:
# Windows PowerShell 5.1 doesn't escape them when it passes an argument to docker.
$script = @'
set -e; mode=$1; backend=$2; old=$3; new=$4; src=/old/$5
if [ -e $src ]; then
  if [ $mode = check ]; then echo $backend: would move $old/$5 to $new; exit 0; fi
  if [ -d $src ]; then cp -a $src/. /new/; else cp -p $src /new/; fi
  chown -R 1000:1000 /new; rm -rf $src; echo $backend: moved $old/$5 to $new
elif ls -A /new | grep -q .; then echo $backend: already moved to $new
else echo $backend: no login in $old, log in with ops/backends/login.ps1 $backend
fi
[ $mode = check ] || chown 1000:1000 /new
'@ -replace "`r", ''

function Test-Volume([string]$Name) { @(docker volume ls -q) -contains $Name }

if (-not (@(docker image ls -q $Image).Count)) { throw "image $Image not found" }
$moves = [ordered]@{
    claude = @{ Old = "$Prefix-auth-claude"; New = "$Prefix-login-claude"; Path = '.credentials.json' }
    cursor = @{ Old = "$Prefix-auth-cursor"; New = "$Prefix-login-cursor"; Path = 'home/.config/cursor' }
}
$failed = $false
foreach ($backend in $moves.Keys) {
    $m = $moves[$backend]
    if (-not (Test-Volume $m.Old)) { Write-Host "${backend}: no $($m.Old) volume, nothing to move"; continue }
    if ($Check) {
        $new = if (Test-Volume $m.New) { @('-v', "$($m.New):/new:ro") } else { @('--mount', 'type=tmpfs,target=/new') }
        $volumes = @('-v', "$($m.Old):/old:ro") + $new
        $mode = 'check'
    } else {
        docker volume create $m.New | Out-Null
        $volumes = @('-v', "$($m.Old):/old", '-v', "$($m.New):/new")
        $mode = 'move'
    }
    docker run --rm --network none --user 0:0 @volumes $Image sh -c $script sh $mode $backend $m.Old $m.New $m.Path
    if ($LASTEXITCODE -ne 0) { $failed = $true; Write-Warning "${backend}: the move failed; see above, and check $($m.Old) and $($m.New)" }
}
if ($failed) { exit 1 }
