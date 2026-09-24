# Release Notes - v0.11.0

Release date: `2026-04-21`

## Summary

`0.11.0` is the Quantastor HA, storage-view parity, and history-maintenance
release.

The goal of this release is to make the newer runtime model feel operationally
real instead of half-finished: Quantastor HA clusters can now be modeled under
one saved system entry with per-view node targeting, inventory-bound storage
views now behave much more like first-class SMART/history targets, and the
admin sidecar finally has the delete/purge/adopt cleanup tools needed to keep
history sane after system-id changes.

## Highlights

- Quantastor HA support is now cluster-style instead of awkwardly
  single-system-with-peer-hosts:
  - one saved Quantastor system can describe up to three shared-SES HA nodes
  - the admin sidecar can discover node ids and labels from the Quantastor API
  - internal storage views such as left/right SATADOM groups can pin a target
    HA node cleanly
- Storage-view parity improved materially:
  - inventory-bound views lazy-load richer SMART detail on hover/focus
  - SATADOM ATA detail now shows the richer Silicon Motion device statistics
  - storage-view history works cleanly with disk-follow metrics where a strong
    identity match exists
- History maintenance is finally operator-facing in admin:
  - `Delete + Purge History`
  - `Purge Orphaned Data`
  - `Adopt Removed System History`
- The repo/wiki docs and screenshots were refreshed to the current behavior,
  including a rewritten Quantastor HA guide and a new
  `History Maintenance and Recovery` wiki page

## Operator Notes

- Quantastor node discovery is still practical rather than magical. On the
  validated appliance, the API returns node ids and labels but not always
  usable node SSH hosts, so operators may still need to fill the per-node host
  fields manually in admin when they want node-targeted SES or SATADOM access.
- Disk-oriented history metrics can now follow the same physical disk across
  homes automatically when the sidecar has strong identity data, but slot
  events still stay local to the slot you opened. Use adoption when the
  `system_id` itself changed; do not expect it for normal disk movement.
- The measured `snapshot_export_estimate` slowdown remains a known follow-up.
  The user explicitly accepted carrying that work after the release, with
  DB/query profiling as the preferred first pass and RAM caching as a possible
  later mitigation if profiling still points there.

## Validation Snapshot

Latest validated checkpoints recorded during the `0.11.0` wrap-up pass:

- Branch-tip code validation on `2026-04-21`:
  - `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  - Result: `220` passed
- Browser QA matrix on `2026-04-21`:
  - full stack (`enclosure-ui + enclosure-history + enclosure-admin`):
    `8` passed, `1` skipped
  - standalone UI only (`enclosure-ui`, with admin/history stopped):
    `8` passed, `1` skipped
  - UI plus history (`enclosure-ui + enclosure-history`, with admin still
    stopped): `8` passed, `1` skipped
- Docs/media refresh checks on `2026-04-21`:
  - `.\.venv\Scripts\python.exe -m compileall app admin_service history_service tests`
  - `.\.venv\Scripts\python.exe -m compileall scripts`
  - `git diff --check` (clean apart from informational LF/CRLF normalization
    warnings on the Windows worktree)
  - refreshed screenshot scripts:
    - `scripts/capture_readme_screenshots.py`
    - `scripts/capture_history_export_screenshots.py`
    - `scripts/capture_release_workflow_screenshots.py`
- External wiki publish on `2026-04-21`:
  - synced the checked-in `wiki/` tree plus `wiki/images/` into
    `git@github.com:gcs8/truenas-jbod-ui.wiki.git`
  - published commit: `c82ac36`
- Perf note captured during the same pass:
  - `python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate-0.11.0-prep`
  - latest summary:
    - `inventory_force` avg `5488.0 ms`
    - `inventory_cached` avg `27.7 ms`
    - `smart_batch` avg `17.9 ms`
    - `mappings_import_roundtrip` avg `1178.8 ms`
    - `snapshot_export_estimate` avg `30297.4 ms`

## Checked-In Artifacts

Release-facing screenshots were refreshed on `2026-04-21` under:

- `docs/images/screenshots/*-v0.11.0.png`
- `wiki/images/*-v0.11.0.png`

New workflow captures for this release include:

- Quantastor HA SATADOM view on `QSOSN HA`
- admin maintenance flow for purge/adopt cleanup
- refreshed grouped runtime selector state on `archive-core`
- refreshed storage-view history, export dialog, and offline snapshot flow

## Deployment Notes

- App version is now `0.11.0` in the release snapshot.
- Existing operators should re-review:
  - `.env.example`
  - `config/config.example.yaml`
  - `config/profiles.example.yaml`
  - `wiki/Quantastor-Setup.md`
  - `wiki/History-Maintenance-and-Recovery.md`
- Release prep for this cut still included:
  - the broad Python suite
  - Playwright browser QA
  - a fresh `docker compose up -d --build`
  - a fresh README/wiki screenshot pass

## Suggested GitHub Release Intro

`0.11.0` makes the newer runtime and admin workflows feel complete. It adds a
cluster-style Quantastor HA model with per-view node targeting, turns
inventory-bound storage views into much better SMART/history citizens, adds the
delete/purge/adopt maintenance tools for saved history cleanup, and refreshes
the operator docs plus screenshots so the current setup story matches what the
app actually does.
