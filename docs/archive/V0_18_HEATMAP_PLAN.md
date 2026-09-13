# v0.18 Heat Map Mode Plan

## Goal

Add a first-class heat map mode to the main read UI so any live enclosure or
saved storage view can keep its physical layout while coloring each bay by an
operator-selected metric.

The first pass should answer one question quickly:

> Where is the weirdness physically?

This is read-only visualization work. It must not add any new disk-control
surface, LED behavior, or admin write path.

## First-Pass UX

- Add a `Heat Map` mode button in the enclosure panel header.
- When enabled, expose a metric dropdown and an inline legend.
- Keep the existing chassis/bay shape intact; the overlay should be a visual
  shift on the physical tile, not a separate chart or table.
- Show unknown/missing values as neutral gray or subtle hatching, never as
  zero.
- Keep the selected/peer-highlight states visible on top of the heat overlay.
- Keep slot hover useful by appending heat-map value/reason lines to the
  existing tooltip.
- Keep the slot detail drawer unchanged except for optionally reflecting the
  selected heat-map value later.

## First-Pass Metrics

Start with metrics already available from live SMART summaries or the existing
history sidecar:

- `Attention Score`
  - explainable `0-100` score
  - derived from SMART health, temperature, endurance, error counters, missing
    SMART for occupied slots, and obvious unhealthy slot state
  - every non-zero score should have short reasons
- `Temperature`
- `Temperature vs View Avg`
- `Power-On Hours`
- `Lifetime Read`
- `Lifetime Write`
- `Read Rate`
- `Write Rate`
- `Annualized Read`
- `Annualized Write`
- `Read/Write Ratio`
- `Endurance Used`
- `Endurance Remaining`
- `Estimated TBW Left`
- `Media Errors`
- `Predictive Errors`
- `Interface CRC Errors`
- `Unsafe Shutdowns`

Identity/string fields are intentionally excluded from the metric dropdown:
serial, GPTID/persistent ID, make/model, pool, vdev, device name, transport
address, firmware, and similar labels stay in hover/detail surfaces.

## Scaling Rules

First pass:

- Use per-view relative scaling for most continuous metrics so the active
  physical layout has useful contrast.
- Use fixed risk scaling for `Attention Score`.
- Use inverse scaling for "remaining" metrics where a lower value is worse.
- Treat `0` as a real value for counters/rates, but treat missing/null as
  unknown.
- Expose a first-pass `Scale` slider that adjusts color sensitivity around the
  current view range without changing the underlying metric values.

Future scale selectors:

- `Absolute`: fixed thresholds for temperature/endurance/risk.
- `Relative`: percentile/min-max within the current view.
- `Historical`: unusual versus this same slot's prior behavior.
- `Neighbor`: unusual versus nearby bays or same row.

## History and Precompute Strategy

The live UI can compute the first-pass overlay from bounded current data:

- SMART-backed metrics use the existing SMART prefetch/cache path.
- rate metrics use the existing history sidecar scope-history request with a
  window, then compute deltas/rates in the browser for the currently rendered
  view. The rate path requests only the needed raw counter
  (`bytes_read` or `bytes_written`) and sets `event_limit=0`, so selecting
  read/write rate does not pull the full slot-event/history bundle.
- The heat-map header owns its own rate window selector so operators can look
  at `1h`, `24h`, `7d`, `30d`, or longer read/write movement without opening
  the slot history drawer.

That is acceptable for v0.18 first feedback because the view requests are
bounded to visible slots and already capped by history sample limits.

The forward path is a sidecar rollup table so the main UI never has to scan raw
metric samples for fleet-wide comparisons:

- Add `metric_rollups` in the history database.
- Key rollups by `system_id`, `enclosure_key`, `slot`, `metric_name`,
  `window_name`, and `computed_at`.
- Suggested windows: `1h`, `6h`, `24h`, `7d`, `30d`.
- Suggested fields:
  - latest value
  - min/max/avg
  - delta
  - rate per hour
  - sample count
  - stale/missing flags
  - percentile within scope
  - z-score or robust deviation within scope
- Run rollups from the history sidecar after normal fast/slow collection.
- Keep daily/weekly/monthly rollups optional until the hourly/24h path proves
  useful.

## Attention Score v1

Keep it boring and inspectable:

- Start from `0`.
- Add fixed points for concrete conditions.
- Clamp at `100`.
- Store/display reasons as short text chips or tooltip lines.

Candidate scoring:

- non-passing SMART status: `+35`
- critical temperature reached: `+40`
- warning temperature reached: `+24`
- temperature `>= 45 C`: `+15`
- temperature `>= 40 C`: `+8`
- media/predictive/non-medium/read/write errors: up to `+35`
- interface CRC errors: up to `+16`
- endurance used `>= 90%`: `+35`
- endurance used `>= 75%`: `+22`
- endurance remaining `<= 10%`: `+35`
- endurance remaining `<= 25%`: `+22`
- occupied slot with no SMART summary: `+10`
- fault/unmapped/unknown present state: `+12` to `+30`

Do not call this ML or AI. It is an explainable operator heuristic.

## Implementation Checklist

- [x] Add plan/checklist docs and mark heat maps as the selected `0.18.0-dev`
      bite.
- [x] Add heat-map controls to the enclosure panel header.
- [x] Add a frontend metric registry with value extraction, formatting,
      scaling polarity, and reason helpers.
- [x] Add tile overlay styling that works on normal trays, top-loader trays,
      NVMe carrier overlays, boot-media cards, and UniFi drive trays.
- [x] Add bounded history-scope fetches for read/write rate metrics.
- [x] Add a heat-map rate-window selector and color sensitivity slider.
- [x] Add Annualized Read as a first-class SMART summary field derived from
      lifetime read bytes and power-on hours.
- [x] Add Read/Write Ratio as a heat-map metric.
- [x] Add generic timeline scrubbing for heat-map metrics backed by history
      sidecar samples.
- [x] Make heat-map state survive normal render/refresh and reset cleanly on
      system/enclosure/view switch.
- [x] Add tooltip heat-map lines.
- [x] Add focused Playwright coverage for toggling heat map mode and changing
      metrics.
- [x] Run `node --check`, focused browser QA, and a live visual pass.

## Current Status

First live prototype is implemented locally on `2026-05-15`.

- Main UI has a `Heat Map` toggle, metric dropdown, current/timeline mode for
  history-backed metrics, rate-window dropdown, sample scrub slider, color
  scale slider, legend, tile badges, and tooltip lines.
- Backend exposes bounded scope-history helpers for the current physical view.
- Read/write rate metrics use the history sidecar without one request per slot.
- After rate-load feedback, the scoped rate request was narrowed to one counter
  metric with no events. On the local Windows Docker stack, the Archive CORE
  60-slot 24h request measured about `3119.8 ms` for the old full bundle,
  `770.7 ms` for `bytes_written`, and `701.8 ms` for `bytes_read`.
- Live Windows Docker stack reports `0.18.0-dev` for UI, history, and admin.
- Focused heat-map Playwright coverage passes, and the full Playwright suite
  passes with `22` passed and `1` skipped.
- After first operator feedback, the visual layer now uses a tray-shaped
  overlay at roughly `50-75%` opacity, with the inspected value centered and
  more prominent in each bay. Empty bays render as neutral/missing and do not
  contribute fake zeroes to the heat-map scale.
- Operator accepted the revised visual direction.
- Follow-up operator feedback added `Annualized Read`, a visible heat-map
  timeframe control for rate metrics, a `Scale` slider for color sensitivity,
  and a `Read/Write Ratio` heat-map metric.
- Final side-quest feedback promoted the deferred timeline idea into the
  release: history-sidecar-backed heat-map metrics can switch from `Current`
  to `Timeline`, use the selected trailing window, and scrub through available
  samples with a sample-step slider. Timeline mode now opens on the latest
  available sample and lets the operator step backward through the selected
  window. Temperature and Temp vs View Avg are the primary ergonomic target,
  while counter metrics opt into their metric-owned playback behavior.
- `annualized_bytes_read` is now a first-class SMART summary/history metric
  across parser, inventory, sidecar collection, history bundle, and slot-detail
  surfaces. Browser Annualized Read formatting keeps a local fallback for older
  cached payloads.
- Latest validation: `node --check app\static\app.js`,
  `node --check qa\ui-switching.spec.js`, `git diff --check` with expected
  CRLF warnings only, full `python -m pytest -q` with `382` tests, and full
  `npx playwright test` with `24` tests all pass after rebuilding the local
  Docker stack.
- Standalone UI mode was rerun with `enclosure-admin` and
  `enclosure-history` stopped. The main UI rendered `60` slot tiles, the admin
  launch link was hidden, and history-backed heat-map metrics showed
  `History unavailable`.
- Latest live screenshots:
  `artifacts/v0.18.0-heatmap/heatmap-read-write-ratio-live.png`,
  `artifacts/v0.18.0-heatmap/heatmap-temperature-timeline-live.png`, and
  `artifacts/v0.18.0-heatmap/heatmap-history-unavailable-standalone-live.png`.

Remaining before closeout:

- Sidecar rollup tables are deferred unless final operator feedback finds the
  metric-only scope-history reads too slow in normal use.
- Release mechanics: version bump, release commit, tag, GitHub release, GHCR
  verification, wiki sync, and next-dev reopen.

## Deferred

- Persist heat-map UI preferences.
- Add user-selectable scale modes.
- Add neighbor/row-aware deviation metrics.
- Add historical-baseline deviation metrics.
- Add sidecar `metric_rollups` and a rollup maintenance dashboard.
- Add exported/offline snapshot persistence for the selected heat-map mode.
- Add screenshot/docs refresh once the visual direction is accepted.
