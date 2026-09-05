# Segmented history v2

This document defines the long-horizon history layout, migration procedure,
query limits, backup format, and recovery behavior.

## Layout

Segmented history uses:

- one writable hot SQLite database;
- immutable SQLite segments under one private, group-traversable segment directory;
- `catalog.json` as the active-generation commit point;
- `.v1-rollback.sqlite3` as the retained pre-migration rollback snapshot;
- `.migration-pending.json` as the durable operation journal.

The history database and segment directory must share a directory mount. Do not
bind-mount the database file by itself. File mount points are rejected because
they can create independent lock domains across containers or namespaces.

The publisher effective UID must own the hot database. It makes or repairs the
owned segment directory to owner/group inherited from that database with exact
mode `0750`, and atomically publishes immutable segments and `catalog.json` with
the same owner/group and exact mode `0640`. This gives the documented backup UID
read/traverse access through its `APP_GID` supplemental group without granting
write access or world access. Rollback snapshots and pending journals remain
private. A directory owned by another UID or a publisher that does not own the
hot database fails closed; publication does not seize unrelated paths.

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

The production image packages only these bounded segmented-history CLI entry
points under `/app/scripts`:

- `scripts/migrate_segmented_history.py`
- `scripts/rotate_segmented_history.py`
- `scripts/query_segmented_history.py`
- `scripts/seal_history_segment.py`

Every offline read of a quiesced hot database (migration, rotation, sealing, and
the query CLI) opens it immutable, so it never creates `-wal`/`-shm` sidecars
beside a stopped service's database. The query CLI refuses a hot database that
already has sidecars; pass `--live` to read beside a running history service.
Use `--live` whenever the service is running, regardless of sidecars: a deployment
whose filesystem refused WAL mode leaves none, and an immutable open beside a live
writer skips locking and can return torn or stale rows.

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
docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-01-01T00:00:00+00:00 \
  --key-id generation-key-1
```

Apply:

```bash
docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
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
docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --recover-rollback
```

Apply the reported recovery:

```bash
docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
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
docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --rollback

docker compose run --rm --entrypoint python enclosure-history scripts/migrate_segmented_history.py \
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
- Observe the source identity and SQLite sidecar absence twice, with a bounded
  delay after preflight. Abort before rollback or journal creation when a writer
  changes the source or creates a sidecar between observations.
- Allocate monotonic `generation-NNNN`, `segment-NNNN`, and sequence values from
  the authenticated prior catalog. Refuse overflow, collisions, or gaps.
- Retain every prior active segment in the candidate catalog and append exactly
  one new segment. Refuse a 33rd active segment.
- Keep each segment at or below `1,610,612,736` bytes and keep the 5,000-row query
  limit.
- Validate offset-aware timestamps before publication and partition every
  historical row into exactly one of the new segment or complementary hot file.
  Operational cutoffs are snapped down to the containing UTC-day boundary so a
  rollup bucket cannot be split across generations.
- Assert exact row accounting per history table across prior segments, the new
  segment, and hot storage before catalog publication.

No source, catalog, segment, or rollback cleanup occurs when backup evidence,
headroom, path identity, timestamp validity, row accounting, fsync, or digest
verification fails.

The scheduled archive and hot database have different owners. The backup UID
owns each private `0600` archive, while the app UID owns the hot database and
must run the publisher. Do not override `enclosure-backup` to run as the app UID.
It cannot read the private archive. Do not run rotation as the backup UID either;
it does not own the hot database.

After a fresh scheduled FULL backup succeeds, stop the history service cleanly
and verify that the one-shot backup container has exited. Confirm that
`backup-status/scheduled-backup.json` reports the successful run and that the
configured host destination is `backups/scheduled`, corresponding to
`/app/backups/scheduled`. Then stage only its named archive into the existing
history bind. Set these four numeric values to the effective Compose identities
before running the block. The defaults shown match the base Compose file.

```bash
APP_UID=10001
APP_GID=10001
BACKUP_UID=1000
BACKUP_GID=1000
sudo env APP_UID="$APP_UID" APP_GID="$APP_GID" BACKUP_UID="$BACKUP_UID" BACKUP_GID="$BACKUP_GID" python3 - <<'PY'
import hashlib
import json
import os
import re
import stat

ARCHIVE_NAME = re.compile(
    r"jbod-scheduled-backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}"
    r"(?:\.7z|\.tar\.zst\.enc)\Z"
)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC
READ_FLAGS = os.O_RDONLY | NOFOLLOW | CLOEXEC
STAGE_NAME = ".segment-rotation-backup"


def numeric_id(name):
    raw = os.environ.get(name, "")
    if not raw.isdecimal():
        raise SystemExit(f"{name} must be a numeric ID")
    value = int(raw)
    if value <= 0:
        raise SystemExit(f"{name} must be a non-root numeric ID")
    return value


def read_regular(directory_fd, name, limit):
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit("required evidence is not a single regular file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit or os.read(descriptor, 1):
            raise SystemExit("required evidence exceeds its staging limit")
        return content, metadata
    finally:
        os.close(descriptor)


def hash_descriptor(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


app_uid = numeric_id("APP_UID")
app_gid = numeric_id("APP_GID")
backup_uid = numeric_id("BACKUP_UID")
backup_gid = numeric_id("BACKUP_GID")
status_directory = os.open("backup-status", DIRECTORY_FLAGS)
backup_root = os.open("backups", DIRECTORY_FLAGS)
backup_directory = os.open("scheduled", DIRECTORY_FLAGS, dir_fd=backup_root)
history_directory = os.open("history", DIRECTORY_FLAGS)
stage_directory = None
stage_created = False
source = None
staged_name = None
try:
    backup_metadata = os.fstat(backup_directory)
    history_metadata = os.fstat(history_directory)
    if (
        stat.S_IMODE(backup_metadata.st_mode) != 0o700
        or (backup_metadata.st_uid, backup_metadata.st_gid) != (backup_uid, backup_gid)
    ):
        raise SystemExit("scheduled backup directory ownership or mode is invalid")
    if (history_metadata.st_uid, history_metadata.st_gid) != (app_uid, app_gid):
        raise SystemExit("history directory ownership is invalid")

    status_bytes, status_metadata = read_regular(
        status_directory, "scheduled-backup.json", 64 * 1024
    )
    if (
        stat.S_IMODE(status_metadata.st_mode) != 0o640
        or (status_metadata.st_uid, status_metadata.st_gid) != (backup_uid, app_gid)
    ):
        raise SystemExit("scheduled backup status ownership or mode is invalid")
    try:
        status_payload = json.loads(status_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit("scheduled backup status is invalid") from error
    if not isinstance(status_payload, dict):
        raise SystemExit("scheduled backup status is invalid")
    artifact_name = status_payload.get("last_artifact_name")
    expected_size = status_payload.get("last_size_bytes")
    expected_digest = status_payload.get("last_sha256")
    included_groups = status_payload.get("included_groups")
    absent_groups = status_payload.get("last_absent_groups")
    if (
        status_payload.get("enabled") is not True
        or type(status_payload.get("success_count")) is not int
        or status_payload["success_count"] <= 0
        or status_payload.get("last_error_code") is not None
        or not isinstance(included_groups, list)
        or "history_db" not in included_groups
        or not isinstance(absent_groups, list)
        or "history_db" in absent_groups
        or not isinstance(artifact_name, str)
        or ARCHIVE_NAME.fullmatch(artifact_name) is None
        or type(expected_size) is not int
        or expected_size <= 0
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise SystemExit("scheduled backup status lacks complete FULL backup evidence")

    source = os.open(artifact_name, READ_FLAGS, dir_fd=backup_directory)
    source_before = os.fstat(source)
    if (
        not stat.S_ISREG(source_before.st_mode)
        or source_before.st_nlink != 1
        or stat.S_IMODE(source_before.st_mode) != 0o600
        or (source_before.st_uid, source_before.st_gid) != (backup_uid, backup_gid)
    ):
        raise SystemExit("scheduled backup archive ownership or mode is invalid")
    verified_size, verified_digest = hash_descriptor(source)
    source_verified = os.fstat(source)
    if (
        (source_before.st_dev, source_before.st_ino, source_before.st_size, source_before.st_mtime_ns)
        != (source_verified.st_dev, source_verified.st_ino, source_verified.st_size, source_verified.st_mtime_ns)
        or verified_size != expected_size
        or verified_digest != expected_digest
    ):
        raise SystemExit("scheduled backup archive failed integrity verification")
    os.lseek(source, 0, os.SEEK_SET)

    os.mkdir(STAGE_NAME, 0o700, dir_fd=history_directory)
    stage_created = True
    stage_directory = os.open(STAGE_NAME, DIRECTORY_FLAGS, dir_fd=history_directory)
    os.fchown(stage_directory, app_uid, app_gid)
    os.fchmod(stage_directory, 0o700)
    destination = os.open(
        artifact_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC,
        0o600,
        dir_fd=stage_directory,
    )
    staged_name = artifact_name
    copied_size = 0
    copied_digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            copied_digest.update(chunk)
            copied_size += len(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination, view):]
        os.fsync(destination)
        os.fchown(destination, app_uid, app_gid)
        os.fchmod(destination, 0o600)
        staged_metadata = os.fstat(destination)
    finally:
        os.close(destination)

    source_after = os.fstat(source)
    if (
        (source_before.st_dev, source_before.st_ino, source_before.st_size, source_before.st_mtime_ns)
        != (source_after.st_dev, source_after.st_ino, source_after.st_size, source_after.st_mtime_ns)
        or copied_size != expected_size
        or copied_digest.hexdigest() != expected_digest
        or staged_metadata.st_size != expected_size
        or stat.S_IMODE(staged_metadata.st_mode) != 0o600
        or (staged_metadata.st_uid, staged_metadata.st_gid) != (app_uid, app_gid)
    ):
        raise SystemExit("scheduled backup archive changed or failed integrity verification")
    current_status, _ = read_regular(status_directory, "scheduled-backup.json", 64 * 1024)
    if current_status != status_bytes or os.listdir(stage_directory) != [artifact_name]:
        raise SystemExit("scheduled backup status changed or staging is not bounded")
    os.fsync(stage_directory)
    os.fsync(history_directory)
except BaseException:
    if stage_directory is not None and staged_name is not None:
        os.unlink(staged_name, dir_fd=stage_directory)
    if stage_directory is not None:
        os.close(stage_directory)
        stage_directory = None
    if stage_created:
        os.rmdir(STAGE_NAME, dir_fd=history_directory)
    raise
finally:
    if source is not None:
        os.close(source)
    if stage_directory is not None:
        os.close(stage_directory)
    os.close(history_directory)
    os.close(backup_directory)
    os.close(backup_root)
    os.close(status_directory)
print("staged_backup=ok")
PY
```

The staging block refuses an existing staging directory, unsafe names, symlinks,
hard links, wrong owners or modes, incomplete FULL-backup status, source changes,
or a size/SHA-256 mismatch. It does not read or copy the passphrase and prints no
artifact name or digest. The rotation dry run performs the canonical status and
freshness validation again.

Dry-run the next append transaction while the history service remains
quiesced. `enclosure-history` keeps its normal app identity and uses only its
existing history and read-only backup-status mounts:

```bash
docker compose run --rm --entrypoint python enclosure-history scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-08-01T00:00:00+00:00 \
  --key-id generation-key-2 \
  --scheduled-backup-dir /app/history/.segment-rotation-backup \
  --scheduled-backup-status /app/backup-status/scheduled-backup.json
```

Dry-run validates the current directory mode but does not change it or create
transaction artifacts.

Apply only after the dry run succeeds and the history service remains quiesced:

```bash
docker compose run --rm --entrypoint python enclosure-history scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --cutoff 2026-08-01T00:00:00+00:00 \
  --key-id generation-key-2 \
  --scheduled-backup-dir /app/history/.segment-rotation-backup \
  --scheduled-backup-status /app/backup-status/scheduled-backup.json \
  --apply
```

Inspect a pending recovery without changing files, then repeat with `--apply`
only after reviewing the reported phase:

```bash
docker compose run --rm --entrypoint python enclosure-history scripts/rotate_segmented_history.py \
  --source /app/history/history.db \
  --segments-dir /app/history/segments \
  --recover
```

After reviewing the rotation result and preserving any required failure
evidence, remove only the fixed staging directory. Do not broaden this path:

```bash
sudo rm -rf --one-file-system -- history/.segment-rotation-backup
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
by SQLite through that same descriptor. If a legacy split left partial rollups
for one logical bucket in both hot and sealed history, readers merge the rows by
bucket identity, sum counts and values, preserve extrema, and select the latest
counter value instead of returning duplicate points.

## Backup schema 2

A segmented full backup contains:

- the hot member at `history/history.sqlite3`;
- each immutable segment at `history/segments/<segment-id>.sqlite3`;
- generation metadata;
- a complete history catalog with segment sizes, SHA-256 digests, coverage,
  row counts, tombstones, and `supersedes` declarations.

Export pins and verifies the catalog and captures the hot snapshot while holding
the history write lock, then releases that lock before copying immutable segment
bytes. Every copied segment is checked against the pinned catalog digest and size
before packaging.

Import validates the manifest before payload extraction, checks every member
size and digest, validates every SQLite member, constructs a local catalog, and
stages the hot file and complete segment directory. Both adjacent candidates are
closed, fsynced, and recorded with exact names, sizes, SHA-256 values,
device/inode identities, modes, and ownership before the durable hot-adjacent
activation marker is published. Post-marker activation reauthenticates the live
prior and staged candidate immediately before rename, rejects any SQLite sidecar,
uses rename-only publication, and retains exact prior-file and prior-tree records.
Segmented reads reject while the marker exists.

Recovery authenticates every live, staged, and previous artifact before acting.
It restores the complete prior generation when publication stopped before both
candidate targets became live. It finalizes forward only when the live hot file,
complete segment tree, catalog digests, and catalog generation ID all match the
journal. A divergent artifact or replaced marker is preserved and recovery fails
closed. Ordinary commit cleanup applies the same identity-bound authentication to
the live candidate and parked prior generation. The shared history lock and marker
remain held through commit cleanup or rollback cleanup, and the marker is removed
last.

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
reusing an already consumed backup. One non-blocking history write lock covers the
claim, every destructive retention batch, and the completion or release
transition. Rotation and restore therefore cannot replace the generation between
authorization and deletion.

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
