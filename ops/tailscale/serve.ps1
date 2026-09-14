# Publishes the daemon and ntfy on the tailnet over HTTPS (tailnet-only; this is `serve`, not `funnel`).
#   https://tower.your-tailnet.ts.net        -> daemon  127.0.0.1:8100
#   https://tower.your-tailnet.ts.net:8443   -> ntfy    127.0.0.1:8095
# Needs HTTPS certificates enabled for the tailnet (admin console > DNS > HTTPS Certificates).
# The configuration persists across reboots; undo with `tailscale serve reset`.
$ErrorActionPreference = 'Stop'
$ts = 'C:\Program Files\Tailscale\tailscale.exe'

& $ts serve --bg --https=443 http://127.0.0.1:8100
& $ts serve --bg --https=8443 http://127.0.0.1:8095
& $ts serve status
