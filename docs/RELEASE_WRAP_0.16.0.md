# Release Wrap - v0.16.0

Date: `2026-04-27`

## Status

`0.16.0` locks in the first observability pass on top of the `0.15.0`
hardware/runtime baseline.

This is intentionally a deployment-and-operations release more than a new
rendering or platform release.

## What This Release Locks In

- a low-friction optional syslog path that keeps the default operator command
  as plain `docker compose up -d`
- a shared JSON log envelope for UI/history/admin without sacrificing the
  readable local rotating file log
- a passive, collector-agnostic metrics model: Prometheus/OpenMetrics over
  HTTP instead of bundling another container into the stack
- first-pass inventory/cache metrics that are low-cardinality enough to be safe
  in Prometheus-style backends
- checked-in Grafana dashboards that make Windows-vs-Linux comparison and
  history freshness visible immediately

## Validation

Local Windows Docker:

- `325` Python tests passed
- Playwright smoke passed with `15` green / `1` skipped
- QA spec syntax checks passed
- read-path perf remained substantially slower than Linux
- the full local harness still timed out on `snapshot_export_estimate`

Linux dev target:

- the synced current working tree also passed `325` Python tests
- the full Playwright sweep passed with `15` green / `1` skipped
- full perf harness stayed healthy and fast enough to keep Linux as the
  preferred baseline

## What Still Rolls Forward

- investigating and reducing the still-bad local Windows Docker Desktop
  `history_status` and snapshot-export path
- understanding the intermittent `unvr-pro` SSH slow path on the Linux dev
  target without overreacting to one slow UniFi box
- deciding whether alert rules, richer structured perf-event logging, or
  optional host-level `node_exporter` coverage are worth a second observability
  pass later
