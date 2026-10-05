# Sign a subscription backend in, inside its sandbox (docs/phase8a-design.md). Run it yourself in a terminal on this
# PC: the login happens through the provider's own flow, and the credentials stay in a Docker volume that only that
# backend's sandboxes mount. The harness daemon never sees them.
#
#   ops\backends\login.ps1 claude              # claude auth login (paste the code from the browser here)
#   ops\backends\login.ps1 codex               # codex login --device-auth (enter the code on OpenAI's site)
#   ops\backends\login.ps1 cursor              # agent login (open the printed URL)
#   ops\backends\login.ps1 codex -App k-1a2b3c4d   # Codex for one App: its login lives in the App's own volume
#   ops\backends\login.ps1 claude -Status      # show the login state without changing it
#   ops\backends\login.ps1 claude -Logout
#
# Claude Code and Cursor keep the credential apart from their state, so one login (harness-login-<backend>) serves
# the owner's sessions and every App. Codex can't: the owner's login is harness-auth-codex and each App needs its
# own (#371), so Codex stays unavailable to an App until this script has logged it in with -App.
#
# Needs the image (docker build -t agent-harness-cli:1 -f sandbox/cli.Dockerfile sandbox) and the egress proxies
# (docker compose -f ops/egress/compose.yaml up -d --build).
param(
    [Parameter(Mandatory = $true)][ValidateSet('claude', 'codex', 'cursor')][string]$Backend,
    [string]$App = '',
    [switch]$Status,
    [switch]$Logout,
    [string]$Image = 'agent-harness-cli:1'
)
$ErrorActionPreference = 'Stop'

# Dir: where the login volume is mounted. Claude and Cursor get a throwaway state directory in the container.
$spec = @{
    claude = @{ Dir = '/home/agent/.claude-login'
                Env = @('CLAUDE_CONFIG_DIR=/home/agent/.claude', 'CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude-login')
                Login = @('claude', 'auth', 'login', '--claudeai'); State = @('claude', 'auth', 'status'); Out = @('claude', 'auth', 'logout') }
    codex  = @{ Dir = '/home/agent/.codex'; Env = @('CODEX_HOME=/home/agent/.codex')
                Login = @('codex', 'login', '--device-auth'); State = @('codex', 'login', 'status'); Out = @('codex', 'logout') }
    # Cursor's login is $XDG_CONFIG_HOME/cursor/auth.json (~/.config/cursor).
    cursor = @{ Dir = '/home/agent/.config/cursor'; Env = @('NO_OPEN_BROWSER=1')
                Login = @('agent', 'login'); State = @('agent', 'status'); Out = @('agent', 'logout') }
}[$Backend]

if ($App) {
    if ($Backend -ne 'codex') { throw "$Backend has one login for every App; run it without -App" }
    # The daemon's App ids (k-<hex>) are their own volume name part (harness/cli_domains.py domain_slug).
    if ($App -cnotmatch '^[a-z0-9][a-z0-9_.-]{0,47}$' -or $App -cmatch '^h-[0-9a-f]{24}$') { throw "not an App id: $App" }
    $volume = "harness-cli-codex-app-$App"
} elseif ($Backend -eq 'codex') {
    $volume = 'harness-auth-codex'
} else {
    $volume = "harness-login-$Backend"
}
$proxy = "http://harness-egress-${Backend}:8888"
$network = "harness-cli-$Backend"

if (-not (docker image inspect $Image 2>$null)) { throw "image $Image not found; build it first (see the top of this script)" }
if ((docker inspect -f '{{.State.Running}}' "harness-egress-$Backend" 2>$null) -ne 'true') {
    throw "proxy harness-egress-$Backend isn't running; start ops/egress/compose.yaml first"
}
docker volume create $volume | Out-Null
# A volume mounted where the image has no directory starts out root's; hand it to the agent user.
docker run --rm --network none --user 0:0 -v "${volume}:/login" $Image chown 1000:1000 /login
if ($LASTEXITCODE -ne 0) { throw "could not prepare volume $volume" }

$command = if ($Status) { $spec.State } elseif ($Logout) { $spec.Out } else { $spec.Login }

# Build one flat argument list. (Don't assign `if (...) { @('-it') }` to a variable and splat it: PowerShell unwraps a
# one-element array to a string, and splatting a string passes its characters one by one.)
$dockerArgs = [System.Collections.Generic.List[string]]::new()
$dockerArgs.AddRange([string[]]@('run', '--rm'))
if (-not $Status) { $dockerArgs.Add('-it') }
$dockerArgs.AddRange([string[]]@('--network', $network, '-e', "HTTPS_PROXY=$proxy", '-e', "HTTP_PROXY=$proxy",
                                 '-e', 'NO_PROXY=localhost,127.0.0.1'))
foreach ($e in $spec.Env) { $dockerArgs.AddRange([string[]]@('-e', $e)) }
$dockerArgs.AddRange([string[]]@('-v', "${volume}:$($spec.Dir)", $Image))
$dockerArgs.AddRange([string[]]$command)

& docker @dockerArgs
exit $LASTEXITCODE
