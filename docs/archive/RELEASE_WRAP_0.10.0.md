# Release Wrap - v0.10.0

Date: `2026-04-19`

## Status

`0.10.0` is cut-ready.

The branch-tip validation matrix, optional-sidecar deployment sweep, final
post-QA perf run, and release-facing artifact refresh are complete. This note
captures the release snapshot and the items that intentionally roll to the next
tag.

## What This Cycle Locked In

- the optional admin sidecar is now part of the real operator workflow for:
  - setup
  - SSH/TLS trust
  - runtime restart control
  - config plus history backup and restore
- storage views are now first-class runtime targets instead of second-class
  metadata
- inventory-bound views such as `Boot SATADOMs` and the NVMe carrier now have
  live SMART and history paths
- saved chassis views now persist their own `profile_id`
- the main UI and admin copy now clearly separate:
  - `Live Enclosures`
  - `Saved Chassis Views`
  - `Virtual Storage Views`
- `archive-core` now exposes the separate CORE `Front 24 Bay` chassis as a
  real live discovered enclosure
- the history sidecar is now more resilient on the validated Docker Desktop
  bind-mount path, including readonly SQLite repair and permission
  normalization
- standalone UI mode and UI-plus-history mode were both validated explicitly,
  not just the full three-container stack

## Final Validation Snapshot

- Python unit suite:
  - `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  - `195` passed
- Browser QA:
  - full stack: `8` passed, `1` skipped
  - UI only: `8` passed, `1` skipped
  - UI plus history: `8` passed, `1` skipped
- Final perf profile label:
  - `release-candidate-0.10.0-final-post-qa`
- Final perf summary:
  - `inventory_force` avg `4926.3 ms`
  - `inventory_cached` avg `18.5 ms`
  - `smart_batch` avg `12.7 ms`
  - `mappings_import_roundtrip` avg `1107.9 ms`
  - `snapshot_export_estimate` avg `7145.8 ms`

## Captured Artifacts

The tracked release-facing screenshots were refreshed to `v0.10.0` names under
`docs/images/screenshots/`, with wiki copies mirrored under `wiki/images/`.

New workflow artifacts for this release:

- `admin-setup-v0.10.0.png`
- `live-vs-storage-views-v0.10.0.png`
- `storage-view-history-v0.10.0.png`
- `archive-core-front-24-v0.10.0.png`

The current `archive-core` selector artifact intentionally shows the live and
virtual groups that are actually configured today. A duplicate saved chassis
view is no longer enabled there by default, so the admin capture is the place
that now demonstrates the grouped saved chassis layout catalog.

## What Rolls To The Next Tag

These items look like valid follow-up work, not blockers for the `0.10.0` cut:

- CORE bootstrap backend vs documented `midclt call user.update` alignment
- remaining storage-view disk-detail parity gaps
- deciding whether legacy saved `ses_enclosure` views without a pinned
  `profile_id` should get an explicit migration path
- deciding whether the history-sidecar root runtime guard can be relaxed later
  on Docker Desktop once the readonly-DB self-heal has baked longer
- optional Quantastor endpoint error cleanup beyond the fixed TLS transport bug
- snapshot-export estimate tuning if we want to shrink the
  `collect_slot_histories` cost in a later perf pass
