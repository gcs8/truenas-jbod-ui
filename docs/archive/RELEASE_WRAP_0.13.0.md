# Release Wrap - v0.13.0

Date: `2026-04-21`

## Status

`0.13.0` is validated on the release-prep branch and ready for final cut
mechanics.

The product slice itself is effectively locked: admin-side full-backup scope,
debug-bundle export, demo-builder seeding, UniFi embedded boot-media support,
and the internal-view visual/detail polish are all in place. The screenshot
refresh and broad validation matrix are now done too, so what remains is the
actual release-closeout flow: final commit review, merge, tag, publish, and
reopen on the next dev branch.

## What This Cycle Locked In

- full-backup scope is now operator-selectable instead of fixed, with locked
  secret paths that force encrypted `.7z` export
- the admin sidecar now exposes a separate scrub-capable `Debug Bundle` export
  for frozen support snapshots
- a one-click `demo-builder-lab` seed path now exists for profile/storage-view
  testing without a real appliance
- UniFi UNVR / UNVR Pro systems can now expose `/dev/boot` as an optional
  `Embedded Boot Media` storage view backed by limited `smartctl -d scsi`
  detail
- `Boot SATADOMs` now use photo-backed SATADOM cards and a tuned boot-device
  shelf instead of the older generic internal-media renderer
- inline smartctl parser noise is stripped from hex-only boot-media SCSI
  identifiers before those values reach the hover text or detail drawer

## Current Release-Prep Snapshot

- app and browser-QA package metadata are now bumped to `0.13.0`
- the current release-prep work is isolated on:
  - `codex/v0.13.0-release-prep-2026-04-21`
- release-facing references and tracked screenshots are now refreshed to
  `v0.13.0`
- broad release validation currently reads:
  - Python `unittest`: `250` passed
  - Playwright smoke: `13` passed / `1` skipped
  - local perf harness label: `release-candidate-0.13.0`
- the broad carry-over work in `TODO.md` still looks like next-cycle backlog,
  not blockers for this cut

## What Still Needs To Happen Before The Cut

- review the final commit shape, then merge/tag/publish
- publish the checked-in `wiki/` tree if it changed
- reopen the repo on the next `-dev` kickoff branch after the tag lands

## What Rolls Beyond This Tag

These items still look like later work, not blockers for the `0.13.0` cut:

- a self-contained debug-bundle viewer or replay/import harness
- broader second-pass builder editing beyond the current preset/matrix path
- the remaining shared `ses_enclosure` geometry cleanup
- older CORE bootstrap/backend clarity work
