# Release Notes - v0.13.0

Release date: `2026-04-21`

## Summary

`0.13.0` is the support-bundles, embedded-boot-media, and internal-view polish
release.

The goal of this release is to make the app more usable when operators need to
package up a system for backup or support, while also tightening the first-pass
internal-device views that now sit alongside the larger enclosure workflows.
The admin sidecar can now build fuller encrypted restore bundles, export
scrub-capable debug bundles, and seed a synthetic builder/test system, while
the UniFi UNVR family now exposes the internal boot device as an optional
`Embedded Boot Media` view instead of dropping it from inventory entirely.

## Highlights

- Full-backup scope is no longer fixed:
  - the admin operations panel now exposes clickable `Included Paths` pills
  - locked secret paths such as `config/ssh`, imported TLS trust material, and
    shared `known_hosts` force encrypted portable `.7z` output when selected
  - encrypted imports can restore those same selected secret paths
- Support snapshots are more intentional:
  - the new `Debug Bundle` export captures a frozen support snapshot in temp
    space instead of pretending to be a restore/import path
  - scrub controls are split into `Scrub obvious secrets` and
    `Scrub disk identifiers`
  - the current shape remains an engineer-facing archive, not yet a
    self-contained viewer or replay flow
- Builder and storage-view work is easier to exercise locally:
  - `Add Demo Builder System` now seeds a reusable synthetic
    `demo-builder-lab` system, profile, and sample views
  - that path now reports readable restart-required success/error results
    instead of the earlier opaque failure state
- UniFi UNVR and UNVR Pro now surface internal boot media:
  - `/dev/boot` is kept as a limited Linux inventory candidate for the
    validated UniFi profiles
  - the app exposes that device through an optional single-slot
    `Embedded Boot Media` storage view
  - detail currently comes from limited `smartctl -d scsi -x -j /dev/boot`
    output because the validated device path still does not expose native MMC
    wear counters
- Internal-view presentation got a tighter visual pass too:
  - `Boot SATADOMs` now use photo-backed SATADOM cards instead of the older
    generic boot-media card
  - the boot-device shelf, row height, and overlay spacing were tuned around
    that art direction so the cards fit the live storage view more cleanly

## Operator Notes

- The new full-backup flow is still intentionally selective by default. The
  narrow plaintext scope remains the safe baseline, while selecting locked
  secret paths promotes the export into encrypted `.7z`.
- The debug bundle is still not a restore path. It is intended for frozen
  support review, not for one-click replay/import.
- UniFi `Embedded Boot Media` currently surfaces only limited SCSI-style health
  and identity detail. Native eMMC `EXT_CSD` lifetime fields such as
  `PRE_EOL_INFO` are still not available through the validated host path.
- The SATADOM and embedded-boot media work stays scoped to internal storage
  views. It does not change the shared live `ses_enclosure` renderer.

## Validation Snapshot

Validated on the `codex/v0.13.0-release-prep-2026-04-21` safety branch after a
fresh local Docker rebuild:

- broad Python suite:
  - `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  - result: `250` tests passed
- browser smoke:
  - `npx playwright test`
  - result: `13` passed / `1` skipped
- runtime and syntax sanity:
  - `docker compose --profile history --profile admin up -d --build enclosure-ui enclosure-history enclosure-admin`
  - `.\.venv\Scripts\python.exe -m compileall app admin_service history_service tests`
  - `node --check app/static/app.js`
  - `node --check admin_service/static/admin.js`
- perf harness:
  - `.\.venv\Scripts\python.exe scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate-0.13.0`
  - `inventory_force` avg `5407.7 ms`
  - `snapshot_export_estimate` avg `14886.1 ms`
  - latest local run stayed in the expected Windows-hosted variability band and
    included one large SMART-summary outlier in the export-estimate path rather
    than a clean branch-wide regression
- refreshed `v0.13.0` screenshots captured and staged under:
  - `docs/images/screenshots/`
  - `wiki/images/`

## Checked-In Artifacts

Release-facing artifacts for this cut should include:

- refreshed `v0.13.0` screenshot set for README/wiki references
- `docs/UNVR_NOTES.md` updates covering the optional embedded boot-media view
- admin and history wiki refreshes that explain:
  - restore-grade full backup bundles
  - engineer-facing debug bundles
  - the separate offline snapshot-export flow

## Deployment Notes

- App version for the release commit is `0.13.0`.
- Operators should re-review:
  - `README.md`
  - `docs/RELEASE_CHECKLIST.md`
  - `docs/UNVR_NOTES.md`
  - `wiki/Admin-UI-and-System-Setup.md`
  - `wiki/History-and-Snapshot-Export.md`
- Final release prep for this cut should still include:
  - a final `git status` / commit-shape review
  - external wiki publish if the checked-in `wiki/` tree changed

## Suggested GitHub Release Intro

`0.13.0` rounds out the operator-support side of the app. It adds selectable
encrypted full-backup scope, a separate scrub-capable debug-bundle export, a
seeded synthetic builder system for local profile/storage-view testing, and a
first-pass `Embedded Boot Media` view for UniFi UNVR and UNVR Pro systems. It
also tightens the internal-device presentation layer with photo-backed SATADOM
cards and smaller detail/formatting fixes around boot-media SMART output.
