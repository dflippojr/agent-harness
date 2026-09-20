# Publishes the staging smoke slot on the tailnet over HTTPS (tailnet-only; `serve`, not `funnel`). Issue #129.
#   https://tower.your-tailnet.ts.net:8444   -> staging daemon  127.0.0.1:8101
# Deliberately a separate script from ops/tailscale/serve.ps1: running this never re-publishes, moves, or removes
# production's :443 -> 8100 route or any other production mapping. Undo staging alone with
# `tailscale serve --https=8444 off`.
# Needs HTTPS certificates enabled for the tailnet (admin console > DNS > HTTPS Certificates).
$ErrorActionPreference = 'Stop'
$ts = 'C:\Program Files\Tailscale\tailscale.exe'

& $ts serve --bg --https=8444 http://127.0.0.1:8101
& $ts serve status
