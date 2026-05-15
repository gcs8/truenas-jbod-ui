# Operations, Logging, and Metrics

This page collects the day-two operational pieces: updating containers,
checking service health, reading logs, shipping syslog, scraping metrics, and
using the starter Grafana dashboards.

For first install, use [[Quick Start|Quick-Start]]. For the service map, use
[[Architecture and Services|Architecture-and-Services]].

## Fast Operator Checklist

| Need | Start here |
| --- | --- |
| Confirm a service is alive | `curl http://your-docker-host:8080/livez` |
| Check cached dependency/readiness state | `curl http://your-docker-host:8080/healthz` |
| Update a published-image install | `docker compose pull` then `docker compose up -d` |
| See the exact image on the host | `docker compose images` |
| Follow container logs | `docker compose logs -f` |
| Turn off metrics endpoints | `METRICS_ENABLED=false` |
| Expose history metrics off-host | `HISTORY_BIND_ADDRESS=0.0.0.0` |

## Updating The Published Image

If you use `latest`:

```bash
docker compose pull
docker compose up -d
```

If you pin a tag:

1. change `JBOD_UI_IMAGE` in `.env`
2. pull the new image
3. restart the services

Example:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.18.0
```

```bash
docker compose pull
docker compose up -d
```

Use [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]] for the full
published-image deployment path.

## Health Endpoints

`/livez` is the lightweight container health path. It should answer quickly and
is the right default for Docker health checks.

```bash
curl http://your-docker-host:8080/livez
```

`/healthz` reports cached app readiness and dependency state. It is meant for
operator inspection and dashboards, not as a forced full inventory refresh.

```bash
curl http://your-docker-host:8080/healthz
```

The history and admin sidecars expose their own health endpoints when those
services are running.

## Local Logs

For a simple deployment, start with Docker logs:

```bash
docker compose logs -f
```

To focus one service:

```bash
docker compose logs -f enclosure-ui
docker compose logs -f enclosure-history
docker compose logs -f enclosure-admin
```

Set structured logs when your log collector handles JSON well:

```dotenv
LOG_FORMAT=json
```

The default text format is easier for quick local reads.

## Optional Syslog Shipping

If you want the normal `docker compose up -d` path to ship container logs to a
remote syslog receiver, use a local override file:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Then set the matching keys in `.env`:

```dotenv
LOG_SYSLOG_ADDRESS=udp://syslog.example.local:514
LOG_SYSLOG_FORMAT=rfc5424micro
LOG_SYSLOG_FACILITY=local0
```

After that, the normal default path stays the same:

```bash
docker compose up -d
```

`docker compose` auto-loads `docker-compose.override.yml` beside the default
Compose file. If you are intentionally running a source-build dev setup, adapt
the same override there; that path is for branch testing and app development,
not the normal homelab install.

The app keeps syslog transport generic. Backend-specific parsing belongs on
the receiver side, whether that is Splunk, ELK/Logstash, Graylog, rsyslog, or
syslog-ng.

## Metrics Endpoints

All three services can expose scrape-based Prometheus/OpenMetrics endpoints:

- main UI: `http://your-docker-host:8080/metrics`
- history sidecar: `http://your-docker-host:8081/metrics` after setting
  `HISTORY_BIND_ADDRESS=0.0.0.0`
- admin sidecar: `http://your-docker-host:8082/metrics`

The first pass includes:

- standard Python/process metrics from `prometheus_client`
- shared HTTP request count, in-flight, and latency metrics
- build/version info for the running service
- history-sidecar collector state, tracked-slot counts, and collection-pass
  duration

Disable the endpoints:

```dotenv
METRICS_ENABLED=false
```

Move the endpoint path:

```dotenv
METRICS_PATH=/metrics
```

The history sidecar stays localhost-only by default. To scrape it from another
host:

```dotenv
HISTORY_BIND_ADDRESS=0.0.0.0
```

Small Prometheus example:

```yaml
scrape_configs:
  - job_name: truenas-jbod-ui
    static_configs:
      - targets:
          - your-docker-host:8080
          - your-docker-host:8082
  - job_name: truenas-jbod-history
    static_configs:
      - targets:
          - your-docker-host:8081
```

## Grafana Dashboards

Starter dashboards live under `grafana/dashboards/`:

- `TrueNAS JBOD UI - Backend Overview`
- `TrueNAS JBOD UI - History & Data`

They focus on request/perf health plus collector/data freshness. They do not
pretend the app already exports a full business-metrics model for every disk or
system.

The current dev dashboards assume a Prometheus datasource named
`Prometheus Lab`. If your Grafana instance uses a different datasource, remap
it during import.

## Related Pages

- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
- [[Architecture and Services|Architecture-and-Services]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Troubleshooting]]
