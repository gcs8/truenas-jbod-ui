# Release Notes - v0.19.0

Release date: `2026-05-16`

## Summary

`0.19.0` is the public demo and offline snapshot robustness release.

The normal `Export Snapshot` path can now carry more of a system in one frozen
artifact: selected saved/virtual storage views plus optional whole-system live
enclosure snapshots, with redaction, history, SMART summaries, and adaptive
downsampling applied across the combined export.

The release also adds the first static public demo site. It uses a live-derived
TN Core / Supermicro CSE-946 60-bay sample rendered through the same offline
snapshot exporter, with critical disk identifiers scrambled and no live backend
or admin/write paths.

## Added

- Snapshot export options for selected saved/virtual storage views.
- Snapshot export options for whole-system live enclosure capture.
- Offline snapshot navigation across embedded live enclosures and embedded
  storage views without a live backend.
- Self-contained storage-view artwork in offline/public artifacts, including
  the M.2 carrier and SATADOM visuals.
- A live-derived public demo artifact under `public-demo/index.html`.
- Public demo data for:
  - TN Core / Supermicro CSE-946 60-bay top-loader layout
  - data `raidz2-0` through `raidz2-6`
  - `spare-1`
  - `mirror-8` special members
  - matching empty bays
  - `4x NVMe Carrier Card`
  - `Boot SATADOMs`
- A GitHub Pages workflow for publishing the checked-in `public-demo/`
  directory.
- Static publishability checks for the public demo artifact.
- Playwright coverage that can smoke-test either a freshly generated public
  demo or the checked-in artifact.

## Changed

- The public demo opens with no selected bay and a 7-day preserved history
  window.
- Public demo IDs now use consistent scrambled serial/SAS/NAA/GPTID-style
  values while keeping non-sensitive make/model/capacity and topology detail.
- GitHub-hosted Pages validation uses the checked-in artifact instead of trying
  to rebuild from local live CORE data.

## Fixed

- Heat-map timeline scrubbing in large offline/public-demo artifacts now
  updates existing slot overlays in place and caches prepared timeline samples,
  avoiding full grid rebuilds on every slider tick.
- The NVMe carrier storage view renders visible, clickable M.2 cards under
  `file://` public/offline artifacts.

## Validation Snapshot

Current local validation on
`codex/v0.19.0-kickoff-2026-05-15-post-0.18.0`:

- `python scripts\check_public_demo_artifact.py public-demo`
- `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js`
- `python -m pytest tests\test_snapshot_export.py tests\test_public_demo_fixture.py -q`
- `npx playwright test qa/offline-snapshot.spec.js qa/public-demo.spec.js`
- `python -m compileall app tests scripts`
- `python -m pytest -q` passed with `392` tests
- `npx playwright test` passed with `27` tests after the local admin sidecar
  was started
- rebuilt local Docker dev stack reports `0.19.0` on the main UI `/livez`,
  and history/admin health checks returned `ok`
- `git diff --check`

## Public Demo

The Pages URL is:

- https://gcs8.github.io/truenas-jbod-ui/

The repo Pages source is configured for GitHub Actions. The first deploy runs
after the workflow file is present on `main`.
