# Release Wrap - v0.18.0

Date: `2026-05-15`

## Scope

`0.18.0` is the first read-only heat-map release for the main enclosure UI.

The release keeps the physical bay/tray renderer as the primary experience and
adds a visual analysis layer on top of it. It does not introduce new write
paths, disk controls, LED behavior, or admin workflows.

## What This Release Locks In

- a `Heat Map` mode button in the main enclosure header
- a metric registry that excludes identity/string fields and focuses on numeric
  SMART/history/computed values
- tray-shaped overlays that color the actual bay shape instead of replacing the
  enclosure with a separate chart
- centered heat-map values for the selected metric
- neutral rendering for empty bays and unknown/missing values
- bounded history-backed read/write rate queries that request only the needed
  raw counter metric
- first-class Annualized Read support through parser, inventory, history
  collection, history payloads, and UI display
- Read/Write Ratio and explainable Attention Score metrics
- `Current` / `Timeline` playback for history-backed heat-map metrics, with
  timeline mode opening on the latest sample and scrubbing backward through the
  selected window; after the slider is focused, arrow keys provide fine
  one-sample steps
- clean standalone behavior when history/admin sidecars are stopped

## Validation

Local Windows Docker:

- rebuilt UI/history/admin stack reports `0.18.0` on `/livez`
- `node --check app\static\app.js` passed
- `node --check qa\ui-switching.spec.js` passed
- `git diff --check` passed with only expected CRLF warnings
- `python -m pytest -q` passed with `382` tests
- `npx playwright test` passed with `24` tests
- focused heat-map Playwright coverage passed after adding newest-sample
  timeline coverage and history-unavailable coverage
- standalone UI smoke with `enclosure-admin` and `enclosure-history` stopped
  passed: the main page rendered `60` slot tiles, admin launch was hidden, and
  history-backed heat-map metrics showed `History unavailable`

Live visual evidence:

- `artifacts/v0.18.0-heatmap/heatmap-temperature-panel-bold-live.png`
- `artifacts/v0.18.0-heatmap/heatmap-write-rate-panel-bold-live.png`
- `artifacts/v0.18.0-heatmap/heatmap-read-write-ratio-live.png`
- `artifacts/v0.18.0-heatmap/heatmap-temperature-timeline-live.png`
- `artifacts/v0.18.0-heatmap/heatmap-history-unavailable-standalone-live.png`

## What Still Rolls Forward

- sidecar rollup tables remain deferred. The first pass is fast enough with
  metric-only scope-history reads, so hourly/daily/weekly precompute should
  wait for real usage pressure.
- later heat-map follow-ups can add persisted preferences, absolute/relative/
  historical scale selectors, neighbor deviation metrics, and exported snapshot
  persistence for selected heat-map state.
- after the tag is published, reopen on the next `-dev` branch and keep
  future heat-map ideas in the follow-up queue.
