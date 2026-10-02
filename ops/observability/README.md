# Tempo + Grafana for session traces (#259)

These files add Grafana Tempo to the external observability stack (`D:\Docker\observability-stack`, see
`docs/phase5-results.md`) so harness session traces show up in Grafana next to the Prometheus metrics. The repo
can't edit that stack. Applying them is an owner step. What the harness sends, and how to turn it on, is in
[`docs/observability.md`](../../docs/observability.md).

| File | Goes to | What it is |
|---|---|---|
| `tempo.yaml` | `<stack>/tempo/tempo.yaml` | Tempo single-binary config: OTLP/HTTP receiver on 4318, local storage |
| `docker-compose.tempo.yml` | merge into `<stack>/docker-compose.yml` | the `tempo` service; OTLP published on `127.0.0.1:4318` only |
| `grafana-datasource-tempo.yaml` | `<stack>/grafana/provisioning/datasources/` | Tempo datasource (uid `tempo`) with traces-to-metrics links to Prometheus |
| `dashboard-agent-harness-traces.json` | `<stack>/grafana/.../dashboards/` (next to `agent-harness.json`) | "Agent Harness traces": sessions, slow turns, long waits, failed tool executions, all on the Tempo datasource |

## Apply

1. Copy `tempo.yaml` to `<stack>/tempo/tempo.yaml` and add the `tempo` service and `tempo-data` volume from
   `docker-compose.tempo.yml` to the stack's compose file. Put it on the same network as Grafana.
2. Copy `grafana-datasource-tempo.yaml` into Grafana's datasource provisioning directory. If the Prometheus
   datasource's uid isn't `prometheus`, change `tracesToMetrics.datasourceUid`.
3. Copy the dashboard JSON next to the existing Agent Harness dashboard (same provisioning folder).
4. `docker compose up -d tempo` and restart Grafana. Check that Tempo is ready:
   `docker exec tempo wget -qO- http://localhost:3200/ready`.
5. On the tower: `pip install -r requirements-telemetry.txt`, then in `config/harness.yaml`:

   ```yaml
   telemetry:
     otlp_endpoint: http://127.0.0.1:4318/v1/traces
     service_name: agent-harness
     trace_url_template: "<grafana>/explore?schemaVersion=1&panes=%7B%22t%22:%7B%22datasource%22:%22tempo%22,%22queries%22:%5B%7B%22refId%22:%22A%22,%22datasource%22:%7B%22type%22:%22tempo%22,%22uid%22:%22tempo%22%7D,%22queryType%22:%22traceql%22,%22query%22:%22{trace_id}%22%7D%5D,%22range%22:%7B%22from%22:%22now-7d%22,%22to%22:%22now%22%7D%7D%7D&orgId=1"
   ```

   Replace `<grafana>` with the Grafana URL the phone can open (the tailnet name, not localhost). Restart the
   daemon.
6. Start a session, open its Info tab and follow the Trace link.

Nothing here leaves the tower: the harness exports to loopback, and Tempo keeps its data on the local volume.
