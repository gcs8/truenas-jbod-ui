# Grafana Dashboards

This directory holds starter Grafana dashboards for the first-pass
Prometheus/OpenMetrics slice.

Current dashboards:

- `dashboards/truenas-jbod-ui-backend-overview.json`
- `dashboards/truenas-jbod-ui-history-data.json`

They assume a Prometheus datasource named `Prometheus Lab`, which matches the
current disposable dev Grafana sandbox. If your Grafana instance uses a
different datasource name or UID, update the datasource reference during
import.

Suggested import flow:

1. open Grafana
2. go to `Dashboards` -> `New` -> `Import`
3. upload one of the JSON files from `grafana/dashboards/`
4. choose your Prometheus datasource if Grafana asks for remapping

The dashboards intentionally focus on low-cardinality service metrics that
ship today:

- HTTP request rate, error rate, latency, and in-flight requests
- process memory/CPU visibility per service
- inventory snapshot/source-bundle cache behavior and rebuild timings
- SMART summary cache/source outcomes plus in-memory cache-entry gauges
- history collector running state, pass duration, freshness, and stored sample
  counts

The current dashboard revisions also assume Prometheus static labels for
`deployment` (for example `windows-docker` or `linux-dev`) and expose that as
a Grafana dropdown. The backend dashboard adds a second `system_id` dropdown
for the new inventory/cache metrics so you can compare one or more configured
systems inside a selected deployment set.

They are meant to be a useful first crack, not a final observability story.
