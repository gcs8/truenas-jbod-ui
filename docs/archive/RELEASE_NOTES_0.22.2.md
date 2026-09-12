# Release Notes - v0.22.2

Date: `2026-08-31`

`v0.22.2` publishes the segmented-history architecture already merged to `main` and adds the first safe lifecycle patch for production operation.

## Added

- Segmented history keeps one writable hot SQLite database and reads immutable sealed segments through a bounded catalog.
- Encrypted manifest-v2 FULL backups and restores include the hot database, catalog, and every active segment.
- The history collector can use the durable scheduled FULL-backup status to authorize hot-data retention without attempting the incompatible legacy hot-only snapshot.

## Fixed

- Segmented retention now requires a recent successful scheduled FULL backup that selected and actually contained `history_db`. The status must include a positive success count, archive size, digest, and owned artifact name.
- Backup authorization is persisted in the hot SQLite database as a fail-closed `ready` / `claimed` / `consumed` state. A completed pass cannot be repeated after a service restart, failed passes remain retryable, bounded catch-up can continue, and an interrupted claim requires a newer backup.
- Scheduled FULL-backup status now uses a setgid `2750` directory owned by the explicitly configured app GID and atomic `0640` files, allowing the non-root UI and history services to read successful backup evidence without granting either service write access.
- The release performance harness now follows the mapping-import preview/confirmation contract, so its optional empty-mapping roundtrip remains valid against the current API.
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

Before enabling segmented retention, configure the scheduled encrypted FULL backup to include `history_db`, set `APP_GID` to the numeric non-root application group, prepare the status directory as the backup UID and that app GID with mode `2750`, mount it read-only into the history service, and keep the default 36-hour freshness gate unless the backup schedule requires a reviewed alternative. The writer publishes status as `0640`. Retention fails closed when status evidence is missing, unreadable, stale, malformed, artifact-incomplete, unsafe, failed, or reports `history_db` as absent.

Production deployment still requires a fresh independently verified FULL backup immediately before activation. The release tag is not cut until the strict release-wrap validator and all applicable Docker, browser, restored-QA, performance, CI, image, and publication gates pass.
