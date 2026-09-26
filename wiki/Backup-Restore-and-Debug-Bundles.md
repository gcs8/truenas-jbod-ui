# Backup, restore, and debug bundles

Open Admin on port `8082` to create backups, restore application state, or collect support files.

## Choose the right tool

| Tool | Output | Importable? | Use it for |
| --- | --- | --- | --- |
| `Full Backup` | restore-grade archive | yes | Migration and disaster recovery |
| `Debug Bundle` | support archive | no | Sharing selected, scrubbed diagnostic files |
| `Export Snapshot` | self-contained HTML file | no | Sharing one enclosure or storage view offline |
| `Purge Orphaned Data` | maintenance action | not an export | Removing history for deleted systems |
| `Adopt Removed System History` | maintenance action | not an export | Moving orphaned history to a current saved system ID |

Use `Full Backup` if you may need to restore the data. Use `Debug Bundle` only for inspection. Use `Export Snapshot` when the recipient needs an offline bay map rather than application state.

## Create a full backup

Full Backup exports are encrypted by default. Select the state groups needed for the restore:

- `config/config.yaml`
- `config/profiles.yaml`
- slot mappings and slot-detail cache JSON
- the history SQLite database

These secret-material paths can contain credentials or trust data and remain locked until you choose an encrypted export:

- `config/ssh`
- imported TLS trust bundles
- shared `known_hosts`

Selecting a locked path forces encrypted output: `.tar.zst.enc` when the backup includes the history database and the file format is `tar.zst` (the default), otherwise `.7z`. The Admin service rejects an unencrypted export of unsanitized state unless `ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true` is set. That override creates a sensitive plaintext archive. It does not make the archive safe to share.

The admin service sends the 7z passphrase through a private, bounded terminal prompt. It does not place the passphrase in process arguments or command output. The passphrase may contain spaces, including trailing spaces, but it cannot contain carriage returns or line feeds.

## Restore a backup

For a migration:

1. Export a full backup from the source deployment.
2. Start the target Docker deployment with separate local directories.
3. Choose the bundle in Admin on the target deployment.
   Supply the original passphrase if the archive is encrypted.
4. Select `Import Backup`. The Admin service inspects the exact bytes and shows
   the observed encryption mode, selected groups, and aggregate counts.
5. Confirm that inspection. Import then requires the short-lived, single-use
   receipt and rehashes the same bytes before any service stop or restore parse.
6. Restart the main UI and any enabled sidecars.
7. Check `/livez`, the runtime selector, one live enclosure, and one history drawer or dashboard view.

Run the first restore in a disposable stack with separate ports and state directories. Do not test import, restore, purge, adopt, delete, or runtime overrides against a long-running deployment unless you intend to change it.

### Which backups this deployment accepts

A backup carries the app version that wrote it. Restore compares that version
with the running one **before it extracts or replaces anything**:

- **Newer than this deployment**: refused, with
  `This backup was made by v0.24.0; this deployment is v0.23.0. Upgrade before
  restoring.` Upgrade the target first, then restore. Nothing on disk is
  touched by the refused attempt.
- **Same version**: accepted.
- **Older**: accepted. Inspection shows a note that settings and history are
  brought up to date during restore.
- **No readable version in the manifest**: accepted, and the schema-version
  check still applies.

A backup whose *schema* is newer is refused the same way:
`This backup was made by a newer version of the app (schema version 3). Upgrade
first, then restore.`

A hot-only deployment exports backup schema 1. A deployment with `HISTORY_SEGMENT_CATALOG_PATH` exports schema 2. Schema 2 includes the hot database, every immutable segment, and the complete catalog generation. Restore validates every member and stages the hot database and segment directory as one rollback-capable transaction. The target must configure `HISTORY_SEGMENT_CATALOG_PATH` before restoring schema 2.

Each immutable segment is limited to 1.5 GiB. Allow temporary-disk and archive space for the hot database plus every active segment. Each 7z create, verify, list, or extract operation has a 10-minute limit and uses normal compression with one worker thread.

The base Compose file keeps large temporary workspaces on disk-backed scratch
inside an existing state mount. `TMPDIR` points history work at `/app/history`,
admin work at its dedicated `/app/host-prep` volume, and the one-shot backup
worker at `/app/backups`. The private `/tmp` tmpfs remains available for small
library/runtime files, but full backup and restore archives do not consume that
memory-backed filesystem.

The restore contract is measured for a **1 GiB admin container**. Archive and
AES-GCM processing use **1 MiB** chunks. The 2 GiB non-history expanded archive limit
is identical during export construction, export verification, and import. Large ZIP,
TAR, and decrypted members remain file-backed. Do not raise these limits to
compensate for memory regressions.

## Optional scheduled state backups

For automatic config backups after changes, cron full backups, remote copies and retention, see [Automatic backup archive and remote targets](#automatic-backup-archive-and-remote-targets). The one-shot runner below is unchanged.

Scheduled backups run in a separate one-shot container with no published port, network, or Docker socket. Start that container from a host timer.

The root-compatible base runs backup as `0:0`, matching history ownership for both
new files and root-owned predecessor state. It still drops all capabilities,
uses a read-only container filesystem, and enables no-new-privileges. Access uses
ordinary owner/group permissions, not a root permission bypass. No upgrade step
changes history ownership or makes data world-readable.

The optional `docker-compose.nonroot.yml` overlay and development Compose retain
the separate backup default `1000:1000`. Explicit `BACKUP_UID` and `BACKUP_GID`
always win. Keep an existing prepared non-root deployment's full Compose chain
and explicit IDs on image-only updates. Do not switch its producer to the root
base while retaining a separate backup UID: newly created `root:root 0660` history
is not readable by that UID. Conversely, do not switch an existing backup UID to
root without preparing its private output, passphrase, and status ownership.

If adopting the root-compatible base, use the root backup setup below. If retaining
a separate backup UID, use the prepared non-root overlay and the explicit setup
below. A separate UID cannot access a `root:root 0700` history directory just by
adding a group. Changing only the producer's group also leaves old root-owned
files inaccessible. Neither is an automatic upgrade repair.

The admin application default for automatic stop is `0`. The supplied Compose files set the separately launched Admin service default to `3600` seconds.

A scheduled backup that includes `history_db` uses the encrypted `.tar.zst.enc` stream format by default, including segmented history, or encrypted `.7z` when `BACKUP_FULL_ARCHIVE_FORMAT=7z` (see "Full backup archive format"). A backup without `history_db` uses the native encrypted `.tar.zst.enc` envelope. The restore path accepts every format, including `.7z` backups made by earlier versions.

### Create the passphrase and state directories

Create a private passphrase file under `config/backup-secrets`. Do not put the passphrase in `.env`, a command argument, or a unit file.

```bash
BACKUP_UID=0
BACKUP_GID=0
APP_GID=10001
sudo install -d -o "$BACKUP_UID" -g "$BACKUP_GID" -m 0700 \
  config/backup-secrets backups backups/scheduled
sudo install -d -o "$BACKUP_UID" -g "$APP_GID" -m 2750 backup-status
sudo install -o "$BACKUP_UID" -g "$BACKUP_GID" -m 0600 /dev/null \
  config/backup-secrets/scheduled-backup-passphrase
read -rsp 'Scheduled backup passphrase: ' BACKUP_PASSPHRASE
printf '%s' "$BACKUP_PASSPHRASE" | sudo tee config/backup-secrets/scheduled-backup-passphrase >/dev/null
unset BACKUP_PASSPHRASE
```

The Compose files mount `config/backup-secrets` read-only at `/run/backup-secrets`.

These commands initialize a new backup setup, not an existing passphrase or
archive directory. Preserve existing files on upgrade. For the prepared non-root
overlay or development Compose, substitute `BACKUP_UID=$(id -u)` and
`BACKUP_GID=$(id -g)` before running the initialization commands, then put those
same numeric values in `.env`. Set `APP_GID` to the prepared application group.

### Configure the one-shot runner

Add the complete runner configuration to the ignored local `.env`:

```dotenv
BACKUP_UID=0
BACKUP_GID=0
SCHEDULED_BACKUP_ENABLED=true
SCHEDULED_BACKUP_DIR=/app/backups/scheduled
SCHEDULED_BACKUP_STATUS_FILE=/app/backup-status/scheduled-backup.json
SCHEDULED_BACKUP_RETENTION_COUNT=14
SCHEDULED_BACKUP_INCLUDED_GROUPS_JSON=["config_file","runtime_overrides_file","profile_file","mapping_file","sas_fabric_alias_file","slot_detail_file","history_db"]
SCHEDULED_BACKUP_PASSPHRASE_FILE=/run/backup-secrets/scheduled-backup-passphrase
HISTORY_SEGMENTED_BACKUP_MAX_AGE_SECONDS=129600
```

For the root base, keep `0:0`. For a prepared non-root deployment, replace these
IDs with the values used to initialize the backup directories and passphrase.
Blank IDs use each Compose chain's defaults; copying `.env.example` does not
force a separate UID on the root base. `APP_GID` remains a positive shared-status
group, default `10001`, even when history and backup run as root. Do not set it
to `0` to match root history ownership; the status validator rejects that value.

The backup container keeps its selected UID and GID and receives `APP_GID` as a supplemental group. The setgid `2750` status directory makes atomic status-file replacements inherit that group. The directory must be owned by the selected backup UID and `APP_GID`; validation is unchanged for root. Status files use `0640`. Archives and the passphrase remain private `0600` files.

The segment directory uses exact mode `0750`; segments and `catalog.json` use exact mode `0640`. Their owner and group match the hot history database.

The history database owner owns segment publication. With the root base, history
and backup share that owner. With the prepared non-root overlay, the separate
backup UID reads published segments through `APP_GID` and cannot modify them.
In that overlay, history directories need group traversal, the hot database
needs group read/write, and its parent needs group write for SQLite sidecars.

Do not run migration, sealing, rotation, or recovery as host root when the hot database belongs to the non-root application UID. The publisher rejects a process whose effective UID does not own the hot database. This prevents a root-owned replacement from making the history service read-only.

### Repair old segmented-history permissions

A deployment with a `0600` catalog or segments needs one bounded permission repair
before adopting a separate backup UID. This is explicit non-root setup, not a
requirement for root-owned state with the root-compatible base and root backup.

1. Run `docker compose down`.
2. Verify that the history root, `history.db`, `segments/catalog.json`, and cataloged `segment-*.sqlite3` paths point to the intended directories and regular files.
3. Set the history root owner to `APP_UID:APP_GID` and mode `0770`.
4. Set the writable `history.db` owner and group to the same identity and mode `0660`.
5. Set the segment directory to mode `0750`.
6. Set only the active catalog and cataloged segment files to mode `0640`.
7. Restart the history service.
8. Run a manual full backup and verify its status before allowing retention or rotation.

Do not recursively relax rollback snapshots, pending journals, or unrelated history files.

### Run and verify one backup

Run a backup manually before enabling a timer:

```bash
docker compose --profile backup run --rm enclosure-backup
```

The runner writes private `0600` archives, validates the archive through restore preflight, avoids overwriting an existing name, writes `0640` status, and prunes only files that match its owned filename pattern.

When `history_db` is selected, the runner creates `.tar.zst.enc` (or `.7z` with `BACKUP_FULL_ARCHIVE_FORMAT=7z`). Without `history_db`, it creates a validated inner system backup and encrypts the `.tar.zst.enc` envelope with AES-256-GCM, a per-file salt, and a per-file nonce. Import either format through the admin restore path with the same passphrase.

### Enable the systemd timer

The supplied unit files assume the Compose project is in `/opt/truenas-jbod-ui`. If your deployment uses another directory, update `WorkingDirectory`, `ConditionPathExists`, and `ReadWritePaths` together.

After the manual backup and restore test succeed, install and enable the timer:

```bash
sudo install -m 0644 deploy/systemd/truenas-jbod-system-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/truenas-jbod-system-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now truenas-jbod-system-backup.timer
systemctl list-timers truenas-jbod-system-backup.timer
```

### Understand backup status and retention

The main UI reads a secret-free status file and exports metrics for run count, last success, last failure, age, size, and failure state. Metric labels omit the destination, artifact name, selected groups, error text, and passphrase-file path.

Hot-only history uses a single-SQLite snapshot schedule. Segmented history cannot use that snapshot because it also needs the catalog and immutable segments.

Segmented hot-data retention runs only when the status file records a recent successful encrypted full backup that includes `history_db`. The default maximum age is `129600` seconds, or 36 hours. A valid status also needs a positive run count, archive size, digest, and owned artifact name. Missing, stale, failed, incomplete, or history-excluding status blocks pruning.

Mount the whole history directory writable in the one-shot backup container. Do not file-bind only `history.db`; segmented locking rejects database-file mount points. Size the backup destination and temporary workspace for the hot database and every active segment.

See [Segmented history v2](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/SEGMENTED_HISTORY_V2.md) for migration, recovery, rollback, and catalog procedures.

## Automatic backup archive and remote targets

The backup scheduler is an optional long-running sidecar,
`enclosure-backup-scheduler`. It takes two kinds of backup, keeps a catalogue
of them, copies each one to remote targets you choose, and deletes old copies
according to your retention rules. It is off by default. Nothing changes on an
existing install until you enable a backup class.

The one-shot runner in [Optional scheduled state backups](#optional-scheduled-state-backups)
(`SCHEDULED_BACKUP_*`, started by a host timer) still works exactly as before.
Use one or the other.

### Backup classes

| Class | Contains | When it runs |
| --- | --- | --- |
| `config` | Every backup group except the history database: `config.yaml`, runtime overrides, profiles, slot mappings, SAS fabric aliases and the slot-detail cache | A short while after configuration changes |
| `full` | `config` plus the history database | On a cron schedule |

Both classes are encrypted with the same passphrase file as scheduled backups
and are checked (decrypted and preflighted) before they are catalogued.

#### Config backups on change

The main UI and the Admin service record each configuration save in a change
journal: mapping and alias edits, system add, edit and remove, storage views,
profiles, runtime-behaviour overrides and restores. The scheduler waits until
edits stop for `debounce_seconds` and then takes one backup of the whole burst.
A steady stream of edits still gets a backup after `max_delay_seconds`. If a
burst nets out to no change (you changed a value and then changed it back), no
backup is taken.

Recording a change never fails or slows the save. If the journal cannot be
written, the UI logs a warning and the save goes ahead.

#### Full backups on a schedule

`schedule` is a five-field cron expression (`minute hour day-of-month month
day-of-week`) or one of `@hourly`, `@daily`, `@midnight`, `@weekly` and
`@monthly`. Ranges (`1-5`), lists (`1,15`) and steps (`*/15`) work, and Sunday
is `0` or `7`. When both day-of-month and day-of-week are set, either one
matching runs the backup, as in classic cron. Times are in the container's
time zone, set with `TZ` (default `UTC`).

#### Full backup archive format

`full.archive_format` chooses how a full backup is packed. Both formats are
encrypted with the passphrase file and checked before they are catalogued.

| `archive_format` | Packing | Restores on |
| --- | --- | --- |
| `tar.zst` (default) | tar + Zstandard level 3 (two threads), sealed in 1 MiB AES-256-GCM chunks (`.tar.zst.enc`) | This app version and later only |
| `7z` | 7z, LZMA2, one thread, AES-256 | Every app version, and any 7-Zip |

> **Upgrade note.** New FULL backups use `tar.zst` by default. An older app
> version cannot restore them, and plain 7-Zip cannot open them. Existing `.7z`
> backups keep restoring as before. To keep making `.7z` FULL backups, set
> `BACKUP_FULL_ARCHIVE_FORMAT=7z` (or `backups.full.archive_format: 7z` in
> `config.yaml`). The same variable sets the one-shot `enclosure-backup` job's
> format. In the admin Backup page, pick `7-Zip (.7z)` as the file format
> before exporting an encrypted backup that includes history.

`tar.zst` is much faster for a large history database. On a synthetic 2 GiB
history database (4 CPUs):

| | `7z` | `tar.zst` |
| --- | --- | --- |
| Create | 541 s | 17 s |
| Inspect, verify or extract (each) | 53 s | 33 s |
| Size | 264 MiB | 288 MiB |
| Peak memory (create) | 193 MiB | 95 MiB |

These are whole production operations measured with
`scripts/benchmark_full_backup.py`, with a 1 GiB address-space limit on each
phase.

`7z` spends most of a full backup compressing on one core, and each 7z step
stops after 10 minutes, so a history database of about 2 GiB or more can fail
there. That is why `tar.zst` is the default. Choose `7z` if you may need to
restore on an older app version or open the file with 7-Zip.

The `tar.zst` file checks every 1 MiB chunk before using it, so a wrong
passphrase, a damaged or truncated file, or reordered data is refused before
anything is restored. Restore it through the admin restore path like any other
backup. Run the benchmark yourself with
`scripts/benchmark_full_backup.py --size-gib 2 --allow-large --output-root DIR --format tar.zst-stream`.

### Configure

Put a `backups` section in `config.yaml`. Environment variables override single
values, and `BACKUP_TARGETS_JSON` replaces the whole target list.

```yaml
backups:
  config:
    enabled: true
    debounce_seconds: 30
    max_delay_seconds: 600
    local_keep: 30
    remote_keep: 90          # null means no count limit
    remote_max_age_days: null
  full:
    enabled: true
    schedule: "0 3 * * *"
    archive_format: tar.zst  # default; 7z for older app versions, see "Full backup archive format"
    local_keep: 14
    remote_keep: null
    remote_max_age_days: 90
  targets:
    - target_id: office-nas
      label: Office NAS
      provider: s3
      endpoint_url: https://nas.example.test:9000
      bucket: jbod-backups
      root: jbod
      access_key_id_file: /run/backup-secrets/archive_s3_access_key_id
      secret_access_key_file: /run/backup-secrets/archive_s3_secret_access_key
```

| Environment variable | Overrides |
| --- | --- |
| `BACKUP_CONFIG_ENABLED`, `BACKUP_FULL_ENABLED` | `enabled` |
| `BACKUP_CONFIG_DEBOUNCE_SECONDS`, `BACKUP_CONFIG_MAX_DELAY_SECONDS` | debounce |
| `BACKUP_FULL_SCHEDULE` | `full.schedule` |
| `BACKUP_FULL_ARCHIVE_FORMAT` | `full.archive_format` (`tar.zst`, the default, or `7z`); also the one-shot `enclosure-backup` job's format |
| `BACKUP_CONFIG_LOCAL_KEEP`, `BACKUP_FULL_LOCAL_KEEP` | `local_keep` |
| `BACKUP_CONFIG_REMOTE_KEEP`, `BACKUP_FULL_REMOTE_KEEP` | `remote_keep` (`none` for no limit) |
| `BACKUP_CONFIG_REMOTE_MAX_AGE_DAYS`, `BACKUP_FULL_REMOTE_MAX_AGE_DAYS` | `remote_max_age_days` |
| `BACKUP_TARGETS_JSON` | the whole `targets` list, as a JSON array |
| `BACKUP_ARCHIVE_PASSPHRASE_FILE` | passphrase file (falls back to `SCHEDULED_BACKUP_PASSPHRASE_FILE`) |

A scheduled FULL includes the history database and keeps `14` local copies by
default (one daily copy for two weeks). Catalog-verified local FULLs gradually
replace the standalone daily, weekly and monthly history-sidecar snapshots. The
scheduler retains enough older sidecars that the two sets still provide at least
14 copies during cutover; after 14 usable local FULLs exist, no sidecar copy is
needed for that floor. A lower customized FULL retention therefore leaves the
remaining sidecars in place. Cleanup considers only exact older history snapshot
names; newer files, unrelated files, links and every scheduler archive,
including preserved/pinned archives, remain untouched. Until a catalog-verified
FULL containing history exists, every standalone history snapshot remains and
the history service continues making them. This avoids two permanent backup
sets without reducing the existing 14-copy default.

A mistake stops the scheduler with one plain sentence per problem that names
the key and where it came from, for example
`BACKUP_CONFIG_LOCAL_KEEP in the environment must be 1 or more.`

Target credentials are never written inline. Every secret is a path to a file
(`password_file`, `private_key_file`, `private_key_passphrase_file`,
`access_key_id_file`, `secret_access_key_file`) under the read-only
`./config/backup-secrets` mount (`/run/backup-secrets` in the container). An
inline `password` or `secret_access_key` is rejected.

### Editing from the admin Backups page

**Edit settings** on the Backups page edits the same `backups:` section. The
admin checks the whole section with the scheduler's own rules before saving,
writes `config.yaml` atomically, refuses the save when the file changed since
the editor opened, and records a `backups.policy.save` entry in the config-change
journal. Restart the scheduler to apply a save:
`docker compose --profile backup-scheduler restart enclosure-backup-scheduler`.

- Values set in the environment (the table above) are locked in the editor,
  show the value the scheduler uses, and name their variable. When
  `BACKUP_TARGETS_JSON` is set, targets are read-only. Compose passes the same
  `BACKUP_*` variables to the admin container as to the scheduler, so both see
  the same overrides.
- Credentials stay file-only. For each `*_file` setting the editor shows only
  "Secret file present", "missing or not private", or "not set"; it never shows
  or returns the path or its contents. To replace a secret, put the new file
  under `./config/backup-secrets` (mode `600`) and enter its container path,
  for example `/run/backup-secrets/archive_sftp_key`. The path must be inside
  `/run/backup-secrets` and cannot be the archive passphrase file. Leaving the
  field empty keeps the current file.
- Renaming a target keeps its secret files. Changing where it points (provider,
  host, port, user name, share, bucket, endpoint and similar settings) means you
  must choose its secret files again or clear them, so a saved credential is
  never sent to a new server by accident.

### Targets

| `provider` | Encrypted in transit | Notes |
| --- | --- | --- |
| `sftp` | Yes | Needs `known_hosts_path` and a key or password file. Use SFTP for "SCP" |
| `ftp` | Only with `use_tls: true` | Plain FTP is allowed and labelled unencrypted. The archive itself is always encrypted |
| `smb` | With `smb_encrypt: true` | Needs `share` |
| `s3` | By default (HTTPS); no with an `http://` custom endpoint | S3 and compatible stores: `bucket`, `region`, optional `endpoint_url` |
| `nfs` | No | Needs the NFS overlay below |
| `filesystem` | n/a | An absolute path, for example a mounted USB disk |

A `filesystem` target must be a different place from the local archive
(`BACKUP_ARCHIVE_DIR`, `/app/backups/archive` by default). The scheduler refuses
a root that is the local archive, sits inside it, or contains it, including
through a symlink or a bind mount of the same directory. Otherwise remote
retention could delete the only copy of a backup that local retention keeps.
When a filesystem target is in use, `BACKUP_ARCHIVE_DIR` must be an absolute
path. If the target's path cannot be read at startup (for example an unplugged
USB disk), the scheduler logs a warning and still runs local backups. It will
not use that target until the check passes.

Each backup is copied to every enabled target. If one target fails, the other
targets still get their copy and the local copy is kept.

### Retention and preserve

Retention applies per class and per location. `local_keep` keeps the newest N
local copies. Remote copies keep the newest `remote_keep` and/or those younger
than `remote_max_age_days`. Grooming runs after every backup, and you can
preview and apply it from the admin API.

Some copies are never deleted:

- A preserved (pinned) backup is never deleted and does not count toward keep-N.
  Preserve one before an upgrade or migration and give a reason.
- The newest verified copy of each class at each location is always kept.
- Only catalogued copies are deleted. Files you put in the archive folder
  yourself are left alone.

### Deploy

Run these from the Compose folder. `backup_uid` must be the user the scheduler
runs as: `0` with the base file, or your `BACKUP_UID` (default `1000`) with the
non-root overlay. The status directory must be owned by that user, as for the
one-shot runner, or every backup fails before it starts.

```bash
backup_uid="${BACKUP_UID:-0}"
app_gid="${APP_GID:-10001}"
sudo install -d -o "$backup_uid" -g "$app_gid" -m 0700 ./backups
sudo install -d -o "$backup_uid" -g "$app_gid" -m 2750 ./backup-status
sudo install -d -o "$backup_uid" -g "$app_gid" -m 2770 ./backup-journal ./backup-api
docker compose --profile backup-scheduler up -d enclosure-backup-scheduler
```

If `./backup-status` already exists for the one-shot runner, keep its owner;
both writers must run as the same user.

Folders and permissions (all in the shared application group `APP_GID`):

| Folder | Mode | Written by | Read by |
| --- | --- | --- | --- |
| `backup-journal/` | `2770`, files `0660` | UI, admin, scheduler | scheduler |
| `backup-status/` | `2750`, files `0640` | scheduler | UI (`/healthz`) |
| `backup-api/` | `2770` | scheduler (Unix socket) | admin |
| `backups/` (archive and catalogue) | `0700` | scheduler | scheduler |

The setgid bit keeps new files in `APP_GID`. The journal files are created
`0660` explicitly, so the UI, the Admin service and the scheduler can all
append to them even when they run as different users.

The scheduler serves its API on a Unix socket that only the Admin service
mounts. It publishes no port. The admin UI calls it through
`/api/admin/backups/*`, with the same authentication and origin checks as the
other admin routes.

### NFS targets

Mounting NFS needs `CAP_SYS_ADMIN`, so it is a separate opt-in overlay that
grants the capability to the backup scheduler only:

```bash
docker compose -f docker-compose.yml -f docker-compose.backup-nfs.yml \
  --profile backup-scheduler up -d enclosure-backup-scheduler
```

The share is mounted for the length of one job and unmounted afterwards. The
image includes `nfs-common` for `mount.nfs`. Use NFSv4
(`mount_options: nfsvers=4.2`) so no `rpc.statd` is needed. The overlay also
turns off Docker's default AppArmor profile for this container, because that
profile blocks mounts. The scheduler must run as root (the base default
`BACKUP_UID=0`) to use the capability. No other service gains a capability; a
CI contract test checks this.

### Health

A failed backup, or a failed copy to a remote target, is reported on the main
UI's `/healthz` as `degraded` with a reason such as
`Backup target Office NAS degraded: ...`. It never makes `/healthz` return
503. The next successful run clears it.

### Admin API

All routes are on the Admin service under `/api/admin/backups`: list the
library, show one backup and the changes it captured, verify (re-read and
re-hash), download, preserve and unpreserve, restore (the same inspect-then-
import flow and passphrase headers as an uploaded backup), run a backup now,
preview and apply grooming with a one-time plan token, and test a target.
Artifact and library routes use opaque backup ids; they do not accept or return
an artifact file path. Policy routes accept target paths and secret-file paths,
but secret-file paths and credential contents are never returned.

## Create a debug bundle

Use `Debug Bundle` to capture selected local state for support inspection. It creates a standard archive and cannot be imported as a backup. Capture can stop and restart the UI and history sidecar.

Choose the scrubbing controls before capture:

- `Scrub obvious secrets` removes recognized secret values and keeps locked secret paths disabled.
- `Scrub disk identifiers` masks recognized disk identifiers.

Scrubbing is bounded. Inspect the archive before sharing it. Do not include private keys, trust material, or unreviewed logs unless the recipient needs them and you intend to disclose them.

## Export an enclosure snapshot

`Export Snapshot` in the main UI creates one self-contained HTML file for the selected enclosure or storage view. Its `Redact sensitive IDs` option applies bounded aliases and masks but leaves some operational text and hardware fields intact. Review the file before sharing it.

A snapshot is not a backup and does not contain the full application state. See [[History and Snapshot Export|History-and-Snapshot-Export]].

## Clean up history safely

Before purging or adopting history:

1. Export a full backup if the rows may matter later.
2. Confirm the source and target saved system IDs.
3. Use previews or the least destructive action available.
4. Verify the result in the history drawer or dashboard.

See [[History Maintenance and Recovery|History-Maintenance-and-Recovery]].

## Related pages

- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Architecture and Services|Architecture-and-Services]]
