# Operations, logging, and metrics

Use these procedures to update containers, check service health, inspect logs, ship syslog, and monitor Prometheus metrics.

For installation, see [[Quick Start|Quick-Start]]. For service roles, see [[Architecture and Services|Architecture-and-Services]].

## Common commands

| Task | Command or setting |
| --- | --- |
| Check that the main UI process is alive | `curl http://your-docker-host:8080/livez` |
| Read cached readiness and dependency state | `curl http://your-docker-host:8080/healthz` |
| Update a published-image deployment | `docker compose pull && docker compose up -d` |
| Show deployed images | `docker compose images` |
| Follow all service logs | `docker compose logs -f` |
| Disable metrics endpoints | `METRICS_ENABLED=false` |
| Bind history off-host | `HISTORY_BIND_ADDRESS=0.0.0.0` |

## Update published images

For an unpinned `latest` deployment:

```bash
docker compose pull
docker compose up -d
```

For a pinned deployment, change `JBOD_UI_IMAGE` in `.env`, then pull and recreate the services:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.23.0
```

```bash
docker compose pull
docker compose up -d
```

Run `docker compose images` to confirm the image in use. See [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]] for deployment and rollback procedures.

## Check service health

Use `/livez` for lightweight process health and Docker health checks:

```bash
curl http://your-docker-host:8080/livez
```

Use `/healthz` to inspect cached application readiness and dependency state:

```bash
curl http://your-docker-host:8080/healthz
```

`/healthz` does not force a full inventory refresh. The history and admin sidecars provide their own `/livez` and `/healthz` endpoints while running.

## Read local logs

Follow all services:

```bash
docker compose logs -f
```

Follow one service:

```bash
docker compose logs -f enclosure-ui
docker compose logs -f enclosure-history
docker compose logs -f enclosure-admin
```

The default text format is suited to direct terminal use. For a collector that parses JSON, set:

```dotenv
LOG_FORMAT=json
```

## Correlate requests

The UI, history sidecar, and admin sidecar create a new 32-character lowercase hexadecimal request ID for every inbound HTTP request. The response returns it in `X-Request-ID`.

A caller-provided value never becomes the service's request ID. When valid, it can appear as `parent_request_id`, which lets you follow internal calls across services. Internal clients forward the server-issued ID in `X-Request-ID`.

Completion records contain:

- component and release
- request ID and optional parent request ID
- bounded HTTP method
- normalized route template
- response status
- duration
- exception class

They omit raw paths, query strings, bodies, cookies, authorization headers, user and system identifiers, credentials, exception messages, and stack traces. Request IDs are correlation values, not authentication or authorization tokens.

The raw Uvicorn access log is disabled to avoid duplicate records with unnormalized request targets. Uvicorn lifecycle and error logs remain enabled. Performance warnings reuse the same request ID and normalized route. Bounded stage timing remains in the response's `Server-Timing` header.

For an unhandled exception, the service writes an `http_request_error` record at `ERROR` immediately before the completion record. This error record contains the stack trace and the same request ID as the `500` response. The completion record remains free of stack traces.

## Ship logs to syslog

To send container logs to a syslog receiver, create a local Compose override:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Set the receiver in `.env`:

```dotenv
LOG_SYSLOG_ADDRESS=udp://syslog.example.test:514
LOG_SYSLOG_FORMAT=rfc5424micro
LOG_SYSLOG_FACILITY=local0
```

Then start the deployment normally:

```bash
docker compose up -d
```

Docker Compose loads `docker-compose.override.yml` beside the default Compose file.

UDP syslog sends logs without confidentiality, peer authentication, delivery guarantees, or tamper protection. Decide whether those risks are acceptable for the network path. If you have explicitly classified the path as a trusted, isolated logging network and accept the UDP risks, the example can send there directly. Otherwise, send local container logs through a host collector using an authenticated and encrypted transport. Configure that collector's server identity checks, certificates, queue, and retry behavior for your logging system, and keep a local copy until remote delivery is verified.

Adapt the override separately if you run `docker-compose.dev.yml` for source development. Parse and route the received records in your syslog, Splunk, ELK, Graylog, rsyslog, or syslog-ng configuration.

## Scrape metrics

The services expose Prometheus/OpenMetrics endpoints while metrics are enabled:

- main UI: `http://your-docker-host:8080/metrics`
- history sidecar: `http://your-docker-host:8081/metrics`
- admin sidecar: `http://your-docker-host:8082/metrics`

The history sidecar listens on loopback by default. To scrape it from another host, intentionally bind it off-host:

```dotenv
HISTORY_BIND_ADDRESS=0.0.0.0
```

Binding a service to `0.0.0.0` makes it reachable on every available interface unless host or network controls restrict it. Review that exposure before enabling the setting.

Exported metrics include:

- Python and process metrics from `prometheus_client`
- HTTP request count, in-flight request, and latency metrics
- build and version information
- history collector state, tracked-slot counts, and collection duration
- admin backup inspection and import counts and duration
- scheduled-backup status metrics

Disable all metrics endpoints with:

```dotenv
METRICS_ENABLED=false
```

Change the endpoint path with:

```dotenv
METRICS_PATH=/metrics
```

### Admin backup metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `truenas_jbod_ui_backup_operations_total` | `service`, `operation`, `outcome` | Completed backup inspection and import operations |
| `truenas_jbod_ui_backup_operation_duration_seconds` | `service`, `operation`, `outcome` | Operation duration, including bounded request streaming and private cleanup |

Allowed `operation` values: `inspect`, `import`, or `unknown`. Allowed `outcome` values: `success`, `rejected`, or `error`. Unexpected values use the bounded `unknown` or `error` buckets.

Request IDs never become metric labels. Metric labels also omit paths, archive names, system identifiers, credentials, request content, and exception messages.

### HTTP request labels

`truenas_jbod_ui_http_requests_total` and `truenas_jbod_ui_http_request_duration_seconds` accept these method labels:

- `GET`
- `HEAD`
- `POST`
- `PUT`
- `PATCH`
- `DELETE`
- `OPTIONS`

Every other method becomes `other`. The route label is the matched route template or `unmatched`. Completion logs use the same bounded method value.

### History health metrics

These gauges use only the `service` label:

| Metric | Meaning |
| --- | --- |
| `truenas_jbod_ui_history_collection_interval_seconds` | Configured background collection interval |
| `truenas_jbod_ui_history_collection_consecutive_failures` | Current consecutive background failure count |
| `truenas_jbod_ui_history_collection_failure_backoff_seconds` | Retry delay after the latest failure |
| `truenas_jbod_ui_history_collection_failure_backoff_max_seconds` | Configured retry-delay limit |
| `truenas_jbod_ui_history_smart_failure_evidence_disks` | Deduplicated disks with SMART or predictive-failure evidence in the latest complete evidence pass |
| `truenas_jbod_ui_history_max_temperature_celsius` | Maximum disk temperature in the latest complete evidence pass |
| `truenas_jbod_ui_history_smart_evidence_timestamp_seconds` | Completion time of the evidence pass represented by the SMART and temperature gauges |

The collector deduplicates references to the same physical disk. It updates SMART failure count and maximum temperature only after a complete evidence pass. When a pass is partial, it retains the last complete values and timestamp instead of clearing failure evidence.

### Prometheus example

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

## Configure starter alerts

Copy `prometheus/rules/truenas-jbod-ui-alerts-v1.yml` into your Prometheus configuration and review every threshold. Keep the local copy outside the application checkout so updates do not overwrite site policy.

Load the file from `prometheus.yml`:

```yaml
rule_files:
  - /etc/prometheus/rules/truenas-jbod-ui-alerts-v1.yml
```

Add `truenas_jbod_ui_monitor: required` only to services that must remain available. The admin sidecar normally stops when it is not needed, so do not label it `required` unless you intentionally keep it running.

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

Validate the rules, then reload Prometheus through your normal deployment procedure:

```bash
promtool check rules /etc/prometheus/rules/truenas-jbod-ui-alerts-v1.yml
```

### Routing ownership

The supplied rules use `owner: operator-configure`. Change that value in your local copy to the route name expected by Alertmanager. The application does not choose a paging destination or send notifications.

Route alerts with stable labels such as `owner` and `severity`. Do not add system IDs, enclosure IDs, slots, serials, device names, private addresses, or endpoint labels to notifications.

### Disable or tune a rule

Remove a rule from your local copy to disable it. To tune a rule, change its numeric expression or `for` duration, run `promtool check rules`, and reload Prometheus.

| Alert | Supplied threshold | Review before enabling |
| --- | --- | --- |
| `TrueNASJBODUIServiceUnavailable` | Required target down for 5 minutes | Required targets and expected maintenance duration |
| `TrueNASJBODUIHistoryCollectorStale` | No success for two collection intervals, then 5 minutes | `HISTORY_POLL_INTERVAL_SECONDS`, maintenance windows, and scrape delay |
| `TrueNASJBODUIHistoryCollectionFailures` | Three consecutive failures for 5 minutes | Expected transient API failures and escalation policy |
| `TrueNASJBODUIHistoryBackoffExhausted` | Retry delay reaches `HISTORY_FAILURE_BACKOFF_MAX_SECONDS` for 5 minutes | Backoff setting and escalation policy |
| `TrueNASJBODUISmartFailureEvidence` | One or more disks with SMART failure evidence for 5 minutes | Platform SMART support and response urgency |
| `TrueNASJBODUIHighTemperature` | Maximum observed temperature at or above 55°C for 15 minutes | Vendor limits, airflow, ambient temperature, and normal baseline |

## Import Grafana dashboards

Import the dashboards from `grafana/dashboards/`:

- `TrueNAS JBOD UI - Backend Overview`
- `TrueNAS JBOD UI - History & Data`

They show request performance, service health, collector state, and data freshness. They do not provide per-disk or per-system business metrics beyond the exported series.

The dashboard files refer to a Prometheus datasource named `Prometheus Lab`. Remap the datasource during import if your Grafana instance uses another name.

## Related pages

- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
- [[Architecture and Services|Architecture-and-Services]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Troubleshooting]]
