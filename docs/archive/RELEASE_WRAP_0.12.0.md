# Release Wrap - v0.12.0

Date: `2026-04-21`

## Status

`0.12.0` is validated and ready for final-cut mechanics.

The product work that drove this tag is effectively closed. Builder mode now
exists as a real operator workflow, the Linux host-side runtime no longer needs
the fake `/app/...` harness for repo-local runs, the saved live-backed chassis
parity work is in place, and the export-estimate perf regression already had
its first measured recovery pass. The release-facing closeout is now mostly
done too: docs are refreshed, the `v0.12.0` screenshot set is captured, and
both the local Windows Docker stack plus the Linux dev target have been rerun
through the broad validation matrix. What remains is the actual version bump,
wiki publish, tag cut, and release page work.

## What This Cycle Locked In

- the optional admin sidecar now has a dedicated
  `Enclosure / Profile Builder` workspace instead of forcing profile work into
  the setup sidebar
- custom profiles can now be cloned from the catalog and saved back into
  `profiles.yaml`, updated in place later, or deleted safely when nothing still
  references them
- slot ordering can now be expressed safely through generated presets or an
  explicit `Custom Matrix` editor instead of only by hand-editing YAML
- saved live-backed `ses_enclosure` views now reuse the same profile-driven
  geometry path as their live enclosure backing views for click-off, empty
  detail, row grouping, latch placement, and LED spacing
- host-side Linux runs now resolve config/data/log/history paths relative to
  the checkout instead of requiring a writable `/app/...` harness
- snapshot export estimate recovered after restoring the batched scope-history
  request path and tightening the history-sidecar SQLite temp-store/cache
  settings

## Current Release-Prep Snapshot

- the user has accepted the current builder mode ordering workflow in the live
  browser, including the `Bottom-Up By Columns` pattern and explicit custom
  matrix support
- the Linux dev target has already revalidated:
  - grouped runtime selector behavior on `archive-core`
  - grouped admin `Add Storage View` flow
  - saved live-backed QSOSN HA chassis parity against the live enclosure path
- the refreshed `v0.12.0` screenshot set is now checked in under both
  `docs/images/screenshots/` and `wiki/images/`
- the final validation pass now also has current numbers:
  - local Windows:
    - `234` Python tests passing
    - Playwright `9` passing / `1` skipped
    - perf: `inventory_force 8315.8 ms`, `snapshot_export_estimate 13988.8 ms`
  - Linux dev target:
    - `234` Python tests passing
    - Playwright `10` passing
    - perf: `inventory_force 4619.8 ms`, `snapshot_export_estimate 476.9 ms`
- Linux is now the preferred baseline for follow-up perf work, especially
  because the Windows bind-mounted stack is still much slower on the SMART-heavy
  export-estimate path
- the Linux history sidecar did log a couple of post-restart inventory timeout
  retries against the imported live config during warmup, but it recovered to
  healthy state on its own before the perf run and did not block the browser or
  perf matrix

## What Still Needs To Happen Before The Cut

- publish the checked-in `wiki/` tree if it changed
- bump the version, finalize `CHANGELOG.md`, cut the tag, and publish the
  GitHub release page

## What Rolls Beyond This Tag

These items still look like later work, not blockers for the `0.12.0` cut:

- a more visual tile-level builder editor such as drag-and-drop or inline tile
  editing
- encrypted-export support for SSH keys, imported TLS trust material, and the
  shared `known_hosts` file
- the remaining cleanup pass for any still-split live-vs-saved-vs-virtual
  `ses_enclosure` geometry paths
- the older CORE bootstrap backend/docs mismatch and the remaining
  sidecar/runtime cleanup queue
