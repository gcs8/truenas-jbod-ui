# Release Notes - v0.18.0

Release date: `2026-05-15`

## Summary

`0.18.0` is the heat-map release.

The main read UI can now keep the normal physical enclosure shape and color the
visible bays by an operator-selected metric. The first pass is intentionally
read-only: it helps answer "where is the weirdness physically?" without adding
new disk-control, LED, or admin write behavior.

## Added

- `Heat Map` mode in the enclosure header for live enclosures and saved storage
  views.
- Metric dropdown, inline legend, tray-shaped heat overlay, centered inspected
  values, and hover tooltip lines for the selected metric.
- Heat-map metrics for:
  - Attention Score
  - Temperature
  - Temperature vs View Avg
  - Power-On Hours
  - Lifetime Read / Lifetime Write
  - Read Rate / Write Rate
  - Annualized Read / Annualized Write
  - Read/Write Ratio
  - Endurance Used / Remaining
  - Estimated TBW Left
  - Media, predictive, CRC, and unsafe-shutdown counters
- First-pass `Scale` control for color sensitivity.
- Heat-map-local history window control for history-backed metrics.
- `Current` / `Timeline` mode for metrics backed by history-sidecar samples.
  Timeline mode starts at the latest available sample and the scrub slider steps
  backward through the selected window. After clicking the slider, the left and
  right arrow keys make fine one-sample steps.
- First-class `annualized_bytes_read` support across parser, inventory,
  history-sidecar collection, history store payloads, and the main UI.

## Changed

- Read/write rate heat maps now use bounded scope-history reads that request
  only `bytes_read` or `bytes_written` with `event_limit=0`.
- The color overlay sits on the same tray/bay shape as the enclosure renderer
  at stronger opacity, with the inspected value more prominent in the middle of
  each bay.
- Empty bays and missing metric values render neutral/missing and do not affect
  the heat-map scale.

## Fixed

- Missing timeline sample values are treated as unknown instead of becoming
  fake `0` values.
- History-backed heat-map metrics show `History unavailable` when the optional
  history sidecar is down, and the main UI remains usable as a standalone
  deployment.

## Validation Snapshot

Current local validation on
`codex/v0.18.0-kickoff-2026-05-15-post-0.17.0`:

- `node --check app\static\app.js`
- `node --check qa\ui-switching.spec.js`
- `git diff --check` passed with only expected CRLF warnings
- `python -m pytest -q` passed with `382` tests
- `npx playwright test` passed with `24` tests
- explicit standalone UI smoke passed with `enclosure-admin` and
  `enclosure-history` stopped: the main UI rendered `60` slots, admin launch
  was hidden, and a history-backed heat-map metric showed
  `History unavailable`

## Screenshot Note

The new wiki heat-map feature page includes a tracked `v0.18.0` heat-map
timeline screenshot. Additional live feedback captures remain in local release
evidence under `artifacts/v0.18.0-heatmap/`.
