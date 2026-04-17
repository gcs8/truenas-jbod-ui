# Release Notes Draft - v0.8.0

Release date target: April 16, 2026

## Summary

`0.8.0` adds an optional history sidecar and keeps it integrated into the main
operator workflow instead of turning it into a separate primary UI. The release
also adds frozen offline snapshot export so an enclosure view can be shared in
tickets or audits without exposing live access to the running app.

## Highlights

- Optional SQLite-backed history sidecar started with
  `docker compose --profile history up -d --build`
- Wide slot-history drawer in the main `:8080` UI with:
  - temperature history
  - read/write history
  - total and average-rate chart modes
  - recent change-only events
  - a shared history window picker
- Frozen offline enclosure snapshot export with:
  - self-contained HTML output
  - optional redaction
  - live size estimate
  - ZIP fallback
  - preserved selected slot and history window
- Safer history persistence with:
  - rotating SQLite backups
  - broken-database quarantine
  - weekly and monthly promoted long-term backups by default

## Operator Notes

- The history sidecar is optional. The main UI still works without it.
- If the history sidecar is unavailable:
  - the `History` button stays hidden
  - snapshot export still works
  - snapshot export omits historical charts and events
- Snapshot timestamps render in browser-local time, including exported offline
  snapshots.
- Snapshot exports use the current History drawer window. If you want a
  different range in the exported artifact, change the History window first.

## Deployment Notes

- App version is now `0.8.0`.
- Default history storage paths:
  - live DB: `./history/history.db`
  - short-term rotating backups: `./history/backups`
  - long-term promoted backups: `./history/backups/long-term`
- Default long-term backup policy:
  - keep `4` weekly backups
  - keep `3` monthly backups
- Optional env overrides:
  - `HISTORY_LONG_TERM_BACKUP_DIR`
  - `HISTORY_WEEKLY_BACKUP_RETENTION_COUNT`
  - `HISTORY_MONTHLY_BACKUP_RETENTION_COUNT`

## Recommended Screenshots

- `docs/images/screenshots/history-drawer-v0.8.0.png`
- `docs/images/screenshots/snapshot-export-dialog-v0.8.0.png`
- `docs/images/screenshots/offline-snapshot-v0.8.0.png`

## Suggested GitHub Release Intro

`0.8.0` adds optional historical slot lookback and frozen offline enclosure
snapshot export. The new history sidecar stays separate from the core UI
deployment path, but the main app can surface slot history, change timelines,
and exportable offline snapshots when it is enabled.
