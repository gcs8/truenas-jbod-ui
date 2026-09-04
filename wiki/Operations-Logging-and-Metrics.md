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
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2
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

### Request correlation

The UI, history sidecar, and admin sidecar generate a fresh 32-character
lowercase hexadecimal request ID for every inbound HTTP request. Each response
returns it in `X-Request-ID`. A caller-supplied value never becomes the current
service request ID. A valid value can appear as `parent_request_id` so an
internal call can be followed across services.

Internal UI, history, and admin HTTP clients forward the current server-issued
ID in `X-Request-ID`. Completion records include only the component, release,
request ID, optional parent request ID, method, normalized route template,
status, duration, and exception class. They do not include raw paths, query
strings, bodies, cookies, authorization headers, user or system identifiers,
credentials, exception messages, or stack traces. Treat request IDs as
diagnostic correlation values, not authentication or authorization tokens.
The raw Uvicorn access log is disabled because it would duplicate these records
with an unnormalized request target. Uvicorn lifecycle and error logs remain
enabled. Performance warnings reuse the same request ID and normalized route.
They omit per-request metadata and stage details; bounded stage timing remains
available in the response's `Server-Timing` header.

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
- admin backup inspection and import counts and duration

The admin backup metrics are:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `truenas_jbod_ui_backup_operations_total` | `service`, `operation`, `outcome` | Completed backup inspection and import operations |
| `truenas_jbod_ui_backup_operation_duration_seconds` | `service`, `operation`, `outcome` | End-to-end operation duration, including bounded request streaming and private cleanup |

Allowed `operation` values: `inspect`, `import`, or `unknown`. Allowed
`outcome` values: `success`, `rejected`, or `error`. Unknown values fail into
the bounded `unknown` or `error` buckets. Request IDs never become metric labels.
Paths, archive names, system identifiers, credentials, request content, and
exception messages are also excluded from these metrics.

The starter alert rules use these bounded history-sidecar gauges. Every gauge
has only the `service` application label:

| Metric | Meaning |
| --- | --- |
| `truenas_jbod_ui_history_collection_interval_seconds` | Configured background collection interval |
| `truenas_jbod_ui_history_collection_consecutive_failures` | Current consecutive background failure count |
| `truenas_jbod_ui_history_collection_failure_backoff_seconds` | Retry delay scheduled after the latest failure |
| `truenas_jbod_ui_history_collection_failure_backoff_max_seconds` | Configured retry-delay cap |
| `truenas_jbod_ui_history_smart_failure_evidence_disks` | Deduplicated physical disks with SMART or predictive-failure evidence in the latest complete evidence pass |
| `truenas_jbod_ui_history_max_temperature_celsius` | Maximum disk temperature in the latest complete evidence pass |
| `truenas_jbod_ui_history_smart_evidence_timestamp_seconds` | Completion time of the evidence pass represented by the SMART and temperature gauges |

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

## Starter alert rules

The versioned example rules live at
`prometheus/rules/truenas-jbod-ui-alerts-v1.yml`. They are starting points, not
vendor guarantees. Copy the file into your Prometheus configuration, review
every threshold, and keep your local changes outside the application checkout.

Add the rules file to `prometheus.yml`:

```yaml
rule_files:
  - /etc/prometheus/rules/truenas-jbod-ui-alerts-v1.yml
```

Label only services that must remain available. The admin sidecar normally
stops when it is not needed, so do not give it the `required` label unless your
deployment intentionally keeps it running.

```yaml
scrape_configs:
  - job_name: truenas-jbod-ui
    static_configs:
      - targets:
          - your-docker-host:8080
        labels:
          truenas_jbod_ui_monitor: required
  - job_name: truenas-jbod-history
    static_configs:
      - targets:
          - your-docker-host:8081
        labels:
          truenas_jbod_ui_monitor: required
```

After copying or changing the rules, validate them and reload Prometheus using
your normal deployment process:

```bash
promtool check rules /etc/prometheus/rules/truenas-jbod-ui-alerts-v1.yml
```

Repository CI verifies PromQL syntax with pinned `promtool` 3.5.0, then checks
the exact starter thresholds, durations, labels, privacy boundary, and
documentation contract with
`python -m unittest tests.test_prometheus_alert_rules -v`.

### Routing ownership

The checked-in rules use `owner: operator-configure`. Replace that value in your
local copy with the team or route name your Alertmanager configuration expects.
The application does not choose a paging destination and does not send
notifications itself. Route by stable labels such as `owner` and `severity`.
Do not add system IDs, enclosure IDs, slots, serials, device names, private
addresses, or endpoint labels to notifications.

### Disable or tune a rule

Remove a rule from your local copy to disable it. To tune one, change its
numeric expression or `for` duration, run `promtool check rules`, and reload
Prometheus. Keep the checked-in file unchanged so application updates cannot
overwrite your site policy.

| Alert | Example default | What to review |
| --- | --- | --- |
| `TrueNASJBODUIServiceUnavailable` | Required target down for 5 minutes | Which scrape targets carry `truenas_jbod_ui_monitor="required"` and how long maintenance normally lasts |
| `TrueNASJBODUIHistoryCollectorStale` | No success for two exported collection intervals, then 5 more minutes | `HISTORY_POLL_INTERVAL_SECONDS`, maintenance windows, and scrape delay |
| `TrueNASJBODUIHistoryCollectionFailures` | Three consecutive failures for 5 minutes | Transient API failures and the failure count your team wants to investigate |
| `TrueNASJBODUIHistoryBackoffExhausted` | Retry delay reaches `HISTORY_FAILURE_BACKOFF_MAX_SECONDS` for 5 minutes | Your configured backoff cap and escalation policy |
| `TrueNASJBODUISmartFailureEvidence` | One or more disks with SMART failure or predictive-error evidence for 5 minutes | Platform SMART support and the urgency required for positive evidence |
| `TrueNASJBODUIHighTemperature` | Maximum observed temperature at or above 55°C for 15 minutes | Drive vendor limits, chassis airflow, ambient temperature, and seasonal baseline |

SMART failure count and maximum temperature have only the `service` label. The
collector deduplicates live and saved-view references to the same physical disk.
It updates those gauges only after a complete evidence pass. If a SMART pass is
partial, the previous complete-pass values and
`truenas_jbod_ui_history_smart_evidence_timestamp_seconds` remain in place.
This prevents a missing response from clearing active failure evidence.

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
