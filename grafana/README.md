# Grafana Dashboards

This directory holds starter Grafana dashboards for the Prometheus/OpenMetrics
endpoints the services expose.

Current dashboards:

- `dashboards/truenas-jbod-ui-backend-overview.json`
- `dashboards/truenas-jbod-ui-history-data.json`

The dashboards reference a Prometheus datasource named `Prometheus Lab`. If
your Grafana instance uses a different datasource name or UID, remap it when
Grafana asks during import.

Suggested import flow:

1. open Grafana
2. go to `Dashboards` -> `New` -> `Import`
3. upload one of the JSON files from `grafana/dashboards/`
4. choose your Prometheus datasource if Grafana asks for remapping

The dashboards focus on the low-cardinality service metrics that ship today:

- HTTP request rate, error rate, latency, and in-flight requests
- process memory/CPU visibility per service
- inventory snapshot/source-bundle cache behavior and rebuild timings
- SMART summary cache/source outcomes plus in-memory cache-entry gauges
- history collector running state, pass duration, freshness, and stored sample
  counts

Both dashboards expect a Prometheus static label named `deployment` on each
scrape target and expose it as a Grafana dropdown, so you can keep more than one
installation in the same Prometheus. The backend dashboard adds a second
`system_id` dropdown for the inventory and cache metrics, so you can compare
one or more configured systems inside a selected deployment.

Treat them as a starting point and adjust panels to match your own setup.
