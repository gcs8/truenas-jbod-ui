# Release Notes - v0.17.0

Release date: `2026-05-15`

## Summary

`0.17.0` is the rendering, runtime-control, and history-sidecar polish release
on top of the `0.16.2` hotfix baseline.

The release focuses on making the existing multi-system operator surfaces feel
less surprising:

- live, saved, admin-preview, builder-preview, storage-view-preview, and
  offline snapshot enclosure faces now share the same profile-driven geometry
- the main UI exposes refresh/cache timing more clearly without adding another
  large control surface
- the history sidecar is more responsive, more observable, and less likely to
  turn one slow host into a stuck dashboard
- the admin sidecar owns runtime behavior overrides explicitly, while `.env`
  owned values stay read-only
- backup/restore portability and failure-mode checks now have a documented RC
  path through disposable QA stacks

## Added

- shared frontend geometry adapter/row rendering for live and saved
  `ses_enclosure` chassis views
- shared profile-preview geometry for admin setup, profile builder, and
  storage-view previews
- history dashboard polling for cheap `/healthz` collector state, overview
  counts, DB size, and tracked scopes
- history dashboard `Refresh Fast` and `Refresh Full` actions
- history sidecar perf harness covering `/livez`, `/healthz`, dashboard HTML,
  estimated overview, and opt-in exact counts
- admin runtime-behavior controls that distinguish `.env` owned values from
  admin-owned runtime override values
- comprehensive `v0.17.0` RC QA plan with Windows/Linux disposable restore
  stacks for mutation and failure-mode testing

## Changed

- main UI refresh controls now use a compact toolbar countdown and move
  snapshot/source/SMART/SES timing into cache-status chips
- history startup/manual fast collection now stays cached-root-only for the
  current root scope
- full/slow history collection still performs the deliberately heavier
  forced-inventory sweep, but now exposes enough stage telemetry to understand
  where the time went
- snapshot export estimate/export now uses the same stale-cache-tolerant SMART
  summary path as the main UI
- snapshot export history collection now sends the requested history window to
  the sidecar batch endpoint instead of falling into slow per-slot fallback
  requests
- snapshot export download reuses source inputs staged by the estimate when
  only packaging or oversize choice changes

## Fixed

- history status and snapshot export estimate no longer inherit the old
  batched-history timeout / per-slot fallback storm
- cached SMART misses during fast history collection are bounded to about
  `5 s` and recorded as `smart.failed` instead of setting `last_error`
- one saved-system enumeration failure or one per-scope SMART timeout no longer
  poisons an entire full history pass
- manual history refresh failures now return structured JSON instead of
  leaking plain `Internal Server Error` text into the dashboard
- repeated slow/full history passes skip duplicate DB backups inside
  `HISTORY_BACKUP_INTERVAL_SECONDS`
- history backup pruning is deterministic on fast filesystems because daily
  backup retention now sorts by snapshot filename instead of filesystem mtime

## Validation Snapshot

Current RC validation on `codex/v0.17.0-kickoff-2026-04-27-post-0.16.0`:

- local Python `compileall` passed for app/admin/history/tests/scripts
- full local unit discovery passed with `379` tests; later full local
  `pytest` passed with `380` tests after the export-source-cache and backup
  pruning fixes
- local Playwright passed `22` browser tests
- Windows read-only perf after the history/export fixes:
  - `history_status` about `56.5 ms`
  - `snapshot_export_estimate` about `1432.2 ms`
  - `snapshot_export.collect_slot_histories` about `1864.1 ms`
- Linux disposable RC stack passed:
  - full unit discovery with `373` tests
  - focused history-service suite with `52` tests
  - Playwright with `20` passed / `2` environment-data skips
  - `history_status` about `5.3 ms`
  - `snapshot_export_estimate` about `401.1 ms`
- Windows disposable restore QA stack restored the long-running backup,
  exercised QA-only runtime/config/history mutation and failure-mode checks,
  exported a mutated backup, and re-imported it successfully
- Linux disposable restore QA stack repeated the same restore/mutation/
  failure-mode/export/re-import drill against `codex-dev-test-target`
- public Linux UI/history/admin on `10.13.37.138:8080/8081/8082` reported
  `0.17.0-dev` during RC after the VM reboot and stack refresh
- after the late export-source-cache fix, the public Linux stack was rebuilt
  again, `qa/ui-switching.spec.js` passed `13` tests with `1` data skip against
  `10.13.37.138:8080`, and an Archive CORE 60-bay estimate followed by forced
  ZIP download confirmed the download reused staged inputs instead of
  reloading snapshot/SMART data
- random populated-disk LED identify checks passed on all `7` LED-capable
  system/enclosure targets, and the final forced-refresh verification confirmed
  every tested slot ended with `identify_active=false`
- `v0.17.0` README/wiki screenshots were regenerated after visual acceptance

## Screenshot Note

The README/wiki screenshot set was refreshed with `v0.17.0` images after the
final visual acceptance pass.

## Remaining Release Gates

- release commit, tag, GitHub release, GHCR verification, and external wiki
  sync
