# Release Notes - v0.16.0

Release date: `2026-04-27`

## Summary

`0.16.0` is the first observability-focused release on top of the `0.15.0`
runtime baseline.

The core app flow did not change much for operators, and that was intentional.
This cut is about making the existing UI/history/admin deployment easier to
watch, debug, and compare across hosts without hardwiring the stack to one
logging or metrics backend.

The main themes are:

- optional generic syslog shipping that still works with plain
  `docker compose up -d`
- optional JSON stdout/syslog logs shared across all three services
- scrape-based Prometheus/OpenMetrics endpoints on the UI, history sidecar, and
  admin sidecar
- first-pass low-cardinality inventory/cache metrics
- starter Grafana dashboards for backend/runtime health and history/data
  freshness

## Added

- optional Docker syslog shipping via `docker-compose.override.yml.example`
  plus `.env` knobs for `LOG_SYSLOG_ADDRESS`, `LOG_SYSLOG_FORMAT`, and
  `LOG_SYSLOG_FACILITY`
- optional `LOG_FORMAT=json` support so stdout/syslog can emit one structured
  JSON object per line while the local rotating `logs/app.log` file stays
  human-readable
- shared `/metrics` endpoints backed by `prometheus_client` on:
  - the main UI
  - the history sidecar
  - the admin sidecar
- history-collector metrics for running state, last-success/error timestamps,
  tracked-slot/event/sample counts, and collection-pass duration
- low-cardinality inventory/cache metrics for:
  - snapshot request cache outcomes
  - snapshot rebuild duration
  - source-bundle request cache outcomes
  - source-bundle rebuild duration
  - SMART summary cache/source outcomes
  - in-memory snapshot and SMART cache entry counts
- checked-in Grafana dashboards under `grafana/dashboards/`:
  - `TrueNAS JBOD UI - Backend Overview`
  - `TrueNAS JBOD UI - History & Data`

## Changed

- the history sidecar can now be intentionally rebound with
  `HISTORY_BIND_ADDRESS` when an external scraper needs direct access, while
  the default compose shape still keeps it on localhost
- README, GHCR deployment docs, and the checked-in wiki pages now document the
  syslog + metrics + Grafana path as part of the normal supported deployment
  story instead of leaving observability as an operator-only side quest
- the checked-in Grafana dashboards now expose deployment-aware comparison
  panels, inventory `system_id` filtering where it is safe, and legend tables
  that show `last`, `mean`, `min`, and `max` sorted by highest average first

## Validation Snapshot

Validated on `codex/v0.16.0-kickoff-2026-04-27-post-0.15.0`.

Local Windows Docker:

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  -> `325` tests passed
- `node --check qa/admin-operations.spec.js`
- `node --check qa/esxi-smoke.spec.js`
- `node --check qa/ui-switching.spec.js`
- `npx playwright test` -> `15` passed, `1` skipped
- focused read-path perf harness:
  - `health_cached` about `25.6 ms`
  - `inventory_cached` about `30.2 ms`
  - `storage_views_cached` about `38.8 ms`
  - `history_status` about `6956.4 ms`
  - `mappings_import_roundtrip` about `7235.6 ms`
  - `inventory_force` about `17645.3 ms`
- the full local harness did not finish because `snapshot_export_estimate`
  exceeded the harness's built-in `120s` request timeout

Linux dev target (`codex-dev-test-target`):

- the current working tree was synced to the Ubuntu dev host and rebuilt with
  UI/history/admin enabled
- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`
  -> `325` tests passed
- `npx playwright test` -> `15` passed, `1` skipped
- full perf harness:
  - `health_cached` about `3.6 ms`
  - `inventory_cached` about `9.7 ms`
  - `storage_views_cached` about `24.3 ms`
  - `history_status` about `79.3 ms`
  - `snapshot_export_estimate` about `458.3 ms`
  - `inventory_force` about `9379.4 ms`

## Screenshot Note

No screenshot refresh was needed for this cut.

The shipped app layout and operator-facing workflow story are still represented
accurately by the `v0.15.0` screenshot set, so this release intentionally
reuses those tracked images and focuses the change surface on runtime
observability instead.

## Known Caveat

The Windows-vs-Linux gap is still very real.

Linux-hosted Docker remains the representative runtime/perf baseline for this
project, while local Windows Docker Desktop still shows dramatically slower
`history_status`, `mappings_import_roundtrip`, and snapshot-export behavior.
That is still a follow-up tuning problem, not a correctness regression in the
`0.16.0` feature set.
