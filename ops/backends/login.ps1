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
    cursor = @{ Dir = '/home/agent/.cursor'; Env = @('NO_OPEN_BROWSER=1')
                Login = @('agent', 'login'); State = @('agent', 'status'); Out = @('agent', 'logout') }
}[$Backend]

$volume = "harness-auth-$Backend"
$proxy = "http://harness-egress-${Backend}:8888"

if (-not (docker image inspect $Image 2>$null)) { throw "image $Image not found; build it first (see the top of this script)" }
if ((docker inspect -f '{{.State.Running}}' "harness-egress-$Backend" 2>$null) -ne 'true') {
    throw "proxy harness-egress-$Backend isn't running; start ops/egress/compose.yaml first"
}
docker volume create $volume | Out-Null

$command = if ($Status) { $spec.State } elseif ($Logout) { $spec.Out } else { $spec.Login }
$envArgs = @('-e', "HTTPS_PROXY=$proxy", '-e', "HTTP_PROXY=$proxy", '-e', 'NO_PROXY=localhost,127.0.0.1')
foreach ($e in $spec.Env) { $envArgs += @('-e', $e) }
$tty = if ($Status) { @() } else { @('-it') }

docker run --rm @tty --network harness-cli @envArgs -v "${volume}:$($spec.Dir)" $Image @command
exit $LASTEXITCODE
