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
complete catalog named `generation-0001`. The schema validates `tombstones` and
each replacement segment's `supersedes` list, but v0.22.2 does not publish later
generations automatically. The v0.22.3 generation-2 recovery contract is defined
below. It is a development contract, not authorization to rotate production.

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
no `-wal`, `-shm`, or `-journal` sidecar. Before migrating a database retained
from an older release, start it once through the current history service and
stop the service cleanly. That initialization adds the current tables, columns,
and maintenance triggers. Use an offset-aware ISO-8601 cutoff.

The dry run opens the quiesced source read-only and checks SQLite integrity and
the current schema before creating the segment directory or any migration
artifact. An incompatible legacy database is rejected with an instruction to
initialize it through the current history service. Migration never skips a
missing history table.

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
2. verifies source identity, sidecar absence, SQLite integrity, and current schema;
3. writes and fsyncs a byte-identical v1 rollback snapshot;
4. writes the pending journal;
5. seals and authenticates the immutable segment before publication;
6. stages the complementary hot database using absolute-time comparisons;
7. updates the journal before replacing the hot database;
8. writes and fsyncs the complete catalog;
9. removes the pending journal last.

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

## Later-generation append recovery contract

The first later-generation transaction appends one immutable segment and
publishes one complete catalog generation. It does not delete or rewrite an
existing segment and does not create tombstones or `supersedes` entries.

The transaction owns these artifacts under one exclusive history lock:

| Artifact | Role |
|---|---|
| writable hot database | Active write target before and after the transaction. |
| prior-hot rollback copy | Exact authenticated bytes needed to reverse a pre-commit hot replacement. |
| immutable new segment | Newly sealed historical rows, published under a no-clobber generation name. |
| active `catalog.json` | Commit point that selects the complete readable generation. |
| prior-catalog rollback copy | Exact authenticated previous catalog retained until commit cleanup. |
| staged hot database | Complementary hot rows, built without mutating the active source. |
| staged candidate catalog | Complete prior segment list plus the new segment. |
| hot-adjacent activation journal | Private fsynced state that blocks readers and authenticates recovery. |

The hot database, segment directory, catalog, staging paths, rollback artifacts,
and journal must remain in the same validated directory mount and lock domain.
File bind mounts, symlinked parents, hard-link aliases, or mount aliases fail
before journal creation. The transaction also rejects SQLite `-wal`, `-shm`, and
`-journal` sidecars before copying any bytes. Recovery rejects hard-linked
journals and sidecars beside every immutable segment. Readers recheck both the
activation marker and immutable-segment sidecars on each use, including readers
created before rotation starts.

The activation journal is written at the path returned by
`activation_pending_path(hot_path)`. Existing readers already fail visibly while
that path exists. Before a phase can rely on an artifact, the journal records:

- journal version, operation, transaction ID, and current phase;
- prior and candidate generation IDs;
- canonical relative names for every staged, rollback, and final artifact;
- prior catalog SHA-256 and byte size;
- source hot SHA-256 and byte size;
- prior-hot rollback SHA-256 and byte size after the copy is closed;
- new segment ID, monotonic sequence, final name, SHA-256, byte size, coverage,
  row counts, sealed timestamp, and key ID;
- staged hot SHA-256 and byte size;
- candidate catalog SHA-256 and byte size after final serialization;
- scheduled FULL-backup artifact name, SHA-256, byte size, included-group proof,
  and successful timestamp.

Every journal update is private, atomically replaced, fsynced, and followed by a
parent-directory fsync. Recovery may remove, replace, or promote an artifact only
when its current regular-file bytes match the journal's exact name, size, and
SHA-256. Cleanup first moves the named file into a private quarantine and then
authenticates the moved inode. If the name changed, recovery restores the
replacement and fails closed instead of unlinking it. A file that merely parses
as SQLite or JSON is not authenticated.

### Durable phase state machine

| Phase | Durable state | Recovery outcome |
|---|---|---|
| `prepared` | Journal, verified backup evidence, prior catalog identity, source identity, and closed prior-hot rollback copy are durable. No new final segment is visible. | Keep the prior hot and catalog authoritative. Remove only authenticated staging artifacts, then remove the journal last. |
| `segment-published` | The journal authenticated the final segment name, size, and digest before no-clobber publication; the segment directory was fsynced. Prior hot and catalog remain active. | Remove only the exact journal-authenticated orphan segment and staging artifacts. Keep prior hot and catalog byte-identical. Remove the journal last. |
| `hot-staged` | New segment is durable. Staged complementary hot and candidate catalog bytes are closed, hashed, and recorded. Prior hot and catalog remain active. | Remove authenticated staged files and the new segment. Keep prior hot and catalog byte-identical. Remove the journal last. |
| `hot-replaced` | Live hot matches the staged-hot digest; prior-hot rollback and prior catalog remain preserved; active catalog is still the prior generation. Readers remain blocked. | Restore the authenticated prior hot, remove the authenticated new segment and staged candidate catalog, and retain the prior catalog. Remove the journal last. |
| `catalog-replaced` | Live hot, new segment, and active catalog match the candidate generation; prior hot and catalog are still preserved. Readers remain blocked. | Finalize forward only when every candidate artifact matches the journal and the catalog retains every prior active segment. Any mismatch fails closed without cleanup. |
| `cleanup` | Candidate generation is authenticated and committed. Prior rollback artifacts may remain. | Remove only authenticated prior-generation rollback artifacts. Fsync each containing directory and remove the journal last. |

Before `catalog-replaced`, recovery always returns to the prior generation. At or
after `catalog-replaced`, recovery only finalizes the candidate generation when
all recorded bytes match. It never guesses between generations. A mismatch keeps
the journal and all evidence in place for repair.

### Admission and accounting rules

- Verify the active catalog and every active segment through pinned descriptors.
- Require a recent scheduled FULL backup that selected a present `history_db` and
  whose current archive bytes match the status name, size, and SHA-256. After the
  first rotation, each later rotation requires a strictly newer backup artifact
  than the one recorded in the active catalog.
- Check temporary and final free-space headroom on each affected filesystem
  before copying. Repeat the check before publication if the filesystems differ.
- Allocate monotonic `generation-NNNN`, `segment-NNNN`, and sequence values from
  the authenticated prior catalog. Refuse overflow, collisions, or gaps.
- Retain every prior active segment in the candidate catalog and append exactly
  one new segment. Refuse a 33rd active segment.
- Keep each segment at or below `1,610,612,736` bytes and keep the 5,000-row query
  limit.
- Validate offset-aware timestamps before publication and partition every
  historical row into exactly one of the new segment or complementary hot file.
- Assert exact row accounting per history table across prior segments, the new
  segment, and hot storage before catalog publication.

No source, catalog, segment, or rollback cleanup occurs when backup evidence,
headroom, path identity, timestamp validity, row accounting, fsync, or digest
verification fails.

Dry-run the next append transaction while the history service is quiesced:

```bash
python scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-08-01T00:00:00+00:00 \
  --key-id generation-key-2 \
  --scheduled-backup-dir /app/backups \
  --scheduled-backup-status /app/backup-status/scheduled-backup.json
```

Dry-run validates the current directory mode but does not change it or create
transaction artifacts.

Apply only after the dry run succeeds and the history service remains quiesced:

```bash
python scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-08-01T00:00:00+00:00 \
  --key-id generation-key-2 \
  --scheduled-backup-dir /app/backups \
  --scheduled-backup-status /app/backup-status/scheduled-backup.json \
  --apply
```

Inspect a pending recovery without changing files, then repeat with `--apply`
only after reviewing the reported phase:

```bash
python scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --recover
```

These commands define an offline operator path. They do not authorize a
production rotation or remove the release gates below.

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

In segmented mode, the collector does not call the legacy single-SQLite backup.
Before it removes or rolls up hot rows, it requires the durable scheduled-backup
status to prove a recent successful encrypted FULL backup that selected
`history_db`. Configure both values in `.env`:

```dotenv
SCHEDULED_BACKUP_STATUS_FILE=/app/backup-status/scheduled-backup.json
HISTORY_SEGMENTED_BACKUP_MAX_AGE_SECONDS=129600
```

The Compose history service mounts `backup-status` read-only. For the non-root
overlay, set `APP_GID` to the numeric application group and prepare that host
directory as the backup UID and that app GID with exact mode `2750`. The
one-shot backup writer publishes status as `0640`, verifies that the directory
and inherited file group equal the configured app GID, and refuses any other
directory mode or ownership. The status file must be a regular, non-symlink
file no larger than 64 KiB and must not be group-writable or world-writable. A
successful record requires a positive success count, archive size, digest, and
owned artifact name. Missing, unreadable, malformed, artifact-incomplete,
stale, failed, or history-excluding status blocks retention. A status that
selected `history_db` but reports it in `last_absent_groups` also blocks
retention.

The hot database persists each backup authorization as `ready`, `claimed`, or
`consumed`. A successful bounded pass with `has_more` returns the authorization
to `ready`; a failed pass is also retryable. A completed pass consumes it. A
process crash leaves the claim fail-closed so the next pass requires a newer
successful FULL backup. This durable state prevents a service restart from
reusing an already consumed backup.

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
