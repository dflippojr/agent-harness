# Sign a subscription backend in, inside its sandbox (docs/phase8a-design.md). Run it yourself in a terminal on this
# PC: the login happens through the provider's own flow, and the credentials stay in a Docker volume that only that
# backend's sandboxes mount. The harness daemon never sees them.
#
#   ops\backends\login.ps1 claude            # claude auth login (paste the code from the browser here)
#   ops\backends\login.ps1 codex             # codex login --device-auth (enter the code on OpenAI's site)
#   ops\backends\login.ps1 cursor            # agent login (open the printed URL)
#   ops\backends\login.ps1 claude -Status    # show the login state without changing it
#   ops\backends\login.ps1 claude -Logout
#
# Needs the image (docker build -t agent-harness-cli:1 -f sandbox/cli.Dockerfile sandbox) and the egress proxies
# (docker compose -f ops/egress/compose.yaml up -d --build).
param(
    [Parameter(Mandatory = $true)][ValidateSet('claude', 'codex', 'cursor')][string]$Backend,
    [switch]$Status,
    [switch]$Logout,
    [string]$Image = 'agent-harness-cli:1'
)
$ErrorActionPreference = 'Stop'

$spec = @{
    claude = @{ Dir = '/home/agent/.claude'; Env = @('CLAUDE_CONFIG_DIR=/home/agent/.claude')
                Login = @('claude', 'auth', 'login', '--claudeai'); State = @('claude', 'auth', 'status'); Out = @('claude', 'auth', 'logout') }
    codex  = @{ Dir = '/home/agent/.codex'; Env = @('CODEX_HOME=/home/agent/.codex')
                Login = @('codex', 'login', '--device-auth'); State = @('codex', 'login', 'status'); Out = @('codex', 'logout') }
    # Cursor keeps browser-auth state outside CURSOR_CONFIG_DIR on some releases.
    # Put HOME under the mounted volume so every home-relative auth path survives
    # the disposable login/session container without exposing it to the daemon.
    cursor = @{ Dir = '/home/agent/.cursor'; Env = @('NO_OPEN_BROWSER=1', 'HOME=/home/agent/.cursor/home',
                                                      'CURSOR_CONFIG_DIR=/home/agent/.cursor/config')
                Login = @('agent', 'login'); State = @('agent', 'status'); Out = @('agent', 'logout') }
}[$Backend]

$volume = "harness-auth-$Backend"
$proxy = "http://harness-egress-${Backend}:8888"
$network = "harness-cli-$Backend"

if (-not (docker image inspect $Image 2>$null)) { throw "image $Image not found; build it first (see the top of this script)" }
if ((docker inspect -f '{{.State.Running}}' "harness-egress-$Backend" 2>$null) -ne 'true') {
    throw "proxy harness-egress-$Backend isn't running; start ops/egress/compose.yaml first"
}
docker volume create $volume | Out-Null

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
