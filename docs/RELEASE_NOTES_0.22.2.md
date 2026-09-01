# Release Notes - v0.22.2

Date: `2026-08-31`

`v0.22.2` publishes the segmented-history architecture already merged to `main` and adds the first safe lifecycle patch for production operation.

## Added

- Segmented history keeps one writable hot SQLite database and reads immutable sealed segments through a bounded catalog.
- Encrypted manifest-v2 FULL backups and restores include the hot database, catalog, and every active segment.
- The history collector can use the durable scheduled FULL-backup status to authorize hot-data retention without attempting the incompatible legacy hot-only snapshot.

## Fixed

- Segmented retention now requires a recent successful scheduled FULL backup that selected and actually contained `history_db`.
- Backup authorization is persisted in the hot SQLite database as a fail-closed `ready` / `claimed` / `consumed` state. A completed pass cannot be repeated after a service restart, failed passes remain retryable, bounded catch-up can continue, and an interrupted claim requires a newer backup.
- The immutable deployment helper stages the existing private live `.env` beside candidate Compose files before `docker compose config --quiet`, while preserving the explicit digest override and removing the private staging directory afterward.

## Compatibility

- Blank or whitespace-only `HISTORY_SEGMENT_CATALOG_PATH` retains the v1 hot-only behavior.
- Hot-only deployments continue to use the legacy SQLite snapshot and same-pass retention gate.
- Segmented queries remain bounded to 32 segments and 5,000 rows or disk homes.
- Existing sealed segments remain immutable; the patch does not automate later-generation rotation.

## Deferred work

- Crash-safe generation-2 segment rotation is tracked in issue #124 for v0.22.3.
- SATA/AES enclosure mapping remains in issue #119 and draft PR #121 until real shelf validation is available; it is not part of v0.22.2.

## Upgrade note

Before enabling segmented retention, configure the scheduled encrypted FULL backup to include `history_db`, mount its secret-free status directory read-only into the history service, and keep the default 36-hour freshness gate unless the backup schedule requires a reviewed alternative. Retention fails closed when status evidence is missing, stale, malformed, unsafe, failed, or reports `history_db` absent.

Production deployment still requires a fresh independently verified FULL backup immediately before activation. The release tag is not cut until the strict release-wrap validator and all applicable Docker, browser, restored-QA, performance, CI, image, and publication gates pass.
