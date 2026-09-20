# Publishes the daemon, ntfy, Grafana, and SonarQube on the tailnet over HTTPS (tailnet-only; this is `serve`, not `funnel`).
#   https://tower.your-tailnet.ts.net        -> daemon     127.0.0.1:8100
#   https://tower.your-tailnet.ts.net:8443   -> ntfy       127.0.0.1:8095
#   https://tower.your-tailnet.ts.net:3000   -> Grafana    127.0.0.1:3000
#   https://tower.your-tailnet.ts.net:9000   -> SonarQube  127.0.0.1:9000
# The staging smoke slot (issue #129) is published separately by ops/tailscale/serve-staging.ps1:
#   https://tower.your-tailnet.ts.net:8444   -> staging   127.0.0.1:8101
# This script never serves or removes that staging route, and staging never touches :443.
# Needs HTTPS certificates enabled for the tailnet (admin console > DNS > HTTPS Certificates).
# The configuration persists across reboots; undo with `tailscale serve reset`.
$ErrorActionPreference = 'Stop'
$ts = 'C:\Program Files\Tailscale\tailscale.exe'

& $ts serve --bg --https=443 http://127.0.0.1:8100
& $ts serve --bg --https=8443 http://127.0.0.1:8095
& $ts serve --bg --https=3000 http://127.0.0.1:3000
& $ts serve --bg --https=9000 http://127.0.0.1:9000
& $ts serve status
