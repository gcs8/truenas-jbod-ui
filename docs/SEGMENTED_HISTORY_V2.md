# Segmented history v2

This document defines the long-horizon history layout, migration procedure,
query limits, backup format, and recovery behavior.

## Layout

Segmented history uses:

- one writable hot SQLite database;
- immutable SQLite segments under one private segment directory;
- `catalog.json` as the active-generation commit point;
- `.v1-rollback.sqlite3` as the retained pre-migration rollback snapshot;
- `.migration-pending.json` as the durable operation journal.

The history database and segment directory must share a directory mount. Do not
bind-mount the database file by itself. File mount points are rejected because
they can create independent lock domains across containers or namespaces.

The initial implementation uses one segment named `segment-0001.sqlite3` and a
complete catalog named `generation-0001`. Future generation changes use
`tombstones` and each replacement segment's `supersedes` list. A tombstone is
valid only when it names an active replacement segment that declares the same
superseded segment ID.

## Bounds

- Segment hard limit: `1,610,612,736` bytes, or 1.5 GiB.
- Intended hot-database operating target: about 2 GiB.
- Query segment limit: 32 selected segments.
- Query row limit: 5,000.
- Segment and hot databases are queried independently. SQLite `ATTACH` is not
  used.
- Range queries preselect segments from catalog coverage. The reader validates
  every timestamp and recomputes chronological coverage from the segment before
  it trusts those bounds.
- A query that could require more than 32 segments fails. It never truncates the
  catalog and returns plausible partial history.

## Runtime configuration

Segmented reads are opt-in:

```dotenv
HISTORY_SQLITE_PATH=/app/history/history.db
HISTORY_SEGMENT_CATALOG_PATH=/app/history/segments/catalog.json
```

Leave `HISTORY_SEGMENT_CATALOG_PATH` unset for a v1 hot-only database. Once set,
a missing, invalid, digest-mismatched, or recovery-pending catalog makes history
reads fail visibly. The service does not fall back to hot-only results.

The following operations fail closed while a segmented catalog is configured:

- v1-only history backup and restore;
- system-history deletion;
- orphan-history purge;
- system-history adoption.

Use schema-v2 full backup/restore for segmented history. Tombstone-aware
maintenance must be available before destructive history maintenance is enabled
again.

## Offline migration

Stop or otherwise quiesce the history service. Confirm the SQLite main file has
no `-wal`, `-shm`, or `-journal` sidecar. Use an offset-aware ISO-8601 cutoff.

Dry run:

```bash
python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-01-01T00:00:00+00:00 \
  --key-id generation-key-1
```

Apply:

```bash
python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-01-01T00:00:00+00:00 \
  --key-id generation-key-1 \
  --apply
```

The apply path:

1. takes the shared history lock;
2. writes and fsyncs a byte-identical v1 rollback snapshot;
3. writes the pending journal;
4. seals and authenticates the immutable segment before publication;
5. stages the complementary hot database using absolute-time comparisons;
6. updates the journal before replacing the hot database;
7. writes and fsyncs the complete catalog;
8. removes the pending journal last.

A committed row cannot silently disappear between segment and hot snapshots.
Normal history writes, store initialization, WAL setup, and restore replacement
all use the same lock domain.

## Recovery and rollback

Inspect first without `--apply`.

Interrupted migration recovery:

```bash
python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --recover-rollback
```

Apply the reported recovery:

```bash
python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --recover-rollback \
  --apply
```

Recovery is phase-bound. It restores or finalizes only when the live source
matches a digest allowed by the durable journal. A divergent live database is
preserved and recovery fails. A segment published immediately before a crash is
removed only when its filename, size, digest, inode, and one-link or two-link
publication state match the journal.

Rollback a completed cataloged migration:

```bash
python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --rollback

python scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --rollback \
  --apply
```

Rollback verifies the catalog, every segment digest, and the retained v1 digest
before replacing the hot database. It removes the activated cataloged segment
generation and retains the v1 rollback snapshot.

## Query behavior

All route-level history reads use the segmented reader when a catalog is
configured:

- slot events;
- raw, hourly, and daily metric samples;
- disk-followed metrics and disk homes;
- slot history bundles;
- batched multi-slot scope history;
- global counts, current-scope activity, historical system summaries, and
  total history storage size.

Multi-slot scope reads open each selected database once. Segment files are
opened with no-follow semantics, hashed through a pinned descriptor, and opened
by SQLite through that same descriptor.

## Backup schema 2

A segmented full backup contains:

- the hot member at `history/history.sqlite3`;
- each immutable segment at `history/segments/<segment-id>.sqlite3`;
- generation metadata;
- a complete history catalog with segment sizes, SHA-256 digests, coverage,
  row counts, tombstones, and `supersedes` declarations.

Import validates the manifest before payload extraction, checks every member
size and digest, validates every SQLite member, constructs a local catalog, and
stages the hot file and complete segment directory. Activation uses one rollback
journal plus a durable hot-adjacent activation marker. Segmented reads reject
while that marker exists. The shared history lock and marker remain held through
commit cleanup or rollback cleanup. If either hot-file or segment-directory
activation fails, the previous state is restored before the marker is removed.

Schema 1 imports remain supported for hot-only deployments. Schema 2 import
requires `HISTORY_SEGMENT_CATALOG_PATH` on the target. Multi-gigabyte schema 2
FULL backups use encrypted portable 7z and the file-backed admin/scheduled
paths. The byte-returning compatibility APIs remain capped at 256 MiB; the
file-backed outer archive cap is 6 GiB, with independent member and expanded
data caps. Legacy native scheduled AES-256-GCM envelopes remain readable within
the compatibility byte cap. Locked secret groups require encrypted portable
7z.

## Retention

The writable hot database continues to receive collector writes. Existing raw,
event, hourly, and daily retention settings apply to hot data. Immutable
segments are not edited by retention. Segment compaction or deletion requires a
new complete generation with validated tombstones and `supersedes` metadata.

Do not delete old segment files by hand. Do not edit `catalog.json` in place.

## Release gates

A release that enables segmented history must complete all of these gates:

1. full Python 3.12 and 3.14 suites, Ruff, compile, Node, Docker, and browser
   validation;
2. sabotage tests for marker, symlink, digest, lock, archive, and activation
   failures;
3. exact-byte independent candidate review;
4. encrypted export, mutation, import, and query drill on data larger than the
   current production history database;
5. development stack shutdown and cleanup after the drill;
6. a fresh verified production FULL backup from the admin sidecar;
7. immutable image digest verification before the one production deployment.

Production remains on the predecessor release until every gate passes.
