# Backup, restore, and debug bundles

Open the optional admin sidecar on port `8082` to create backups, restore application state, or collect support files.

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

These secret-material paths can contain credentials or trust data and remain locked until you choose an encrypted portable `.7z` export:

- `config/ssh`
- imported TLS trust bundles
- shared `known_hosts`

Selecting a locked path forces encrypted `.7z` output. The admin sidecar rejects an unencrypted export of unsanitized state unless `ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true` is set. That override creates a sensitive plaintext archive. It does not make the archive safe to share.

The admin service sends the 7z passphrase through a private, bounded terminal prompt. It does not place the passphrase in process arguments or command output. The passphrase may contain spaces, including trailing spaces, but it cannot contain carriage returns or line feeds.

## Restore a backup

For a migration:

1. Export a full backup from the source deployment.
2. Start the target Docker deployment with separate local directories.
3. Choose the bundle in the target admin sidecar.
   Supply the original passphrase if the archive is encrypted.
4. Select `Import Backup`. The admin sidecar inspects the exact bytes and shows
   the observed encryption mode, selected groups, and aggregate counts.
5. Confirm that inspection. Import then requires the short-lived, single-use
   receipt and rehashes the same bytes before any service stop or restore parse.
6. Restart the main UI and any enabled sidecars.
7. Check `/livez`, the runtime selector, one live enclosure, and one history drawer or dashboard view.

Run the first restore in a disposable stack with separate ports and state directories. Do not test import, restore, purge, adopt, delete, or runtime overrides against a long-running deployment unless you intend to change it.

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

Scheduled backups run in a separate one-shot container with no published port, network, or Docker socket. Start that container from a host timer.

The admin application default for automatic stop is `0`. The supplied Compose files set the separately launched admin sidecar default to `3600` seconds.

A scheduled backup that includes `history_db` uses encrypted `.7z`, including segmented history. A backup without `history_db` uses the native encrypted `.tar.zst.enc` envelope. The restore path accepts both formats.

### Create the passphrase and state directories

Create a private passphrase file under `config/backup-secrets`. Do not put the passphrase in `.env`, a command argument, or a unit file.

```bash
BACKUP_UID=$(id -u)
BACKUP_GID=$(id -g)
APP_GID=10001
sudo install -d -o "$BACKUP_UID" -g "$BACKUP_GID" -m 0700 \
  config/backup-secrets backups backups/scheduled
sudo install -d -o "$BACKUP_UID" -g "$APP_GID" -m 2750 backup-status
sudo install -o "$BACKUP_UID" -g "$BACKUP_GID" -m 0600 /dev/null \
  config/backup-secrets/scheduled-backup-passphrase
read -rsp 'Scheduled backup passphrase: ' BACKUP_PASSPHRASE
printf '%s' "$BACKUP_PASSPHRASE" > config/backup-secrets/scheduled-backup-passphrase
unset BACKUP_PASSPHRASE
```

The Compose files mount `config/backup-secrets` read-only at `/run/backup-secrets`.

### Configure the one-shot runner

Add the complete runner configuration to the ignored local `.env`:

```dotenv
BACKUP_UID=1000
BACKUP_GID=1000
SCHEDULED_BACKUP_ENABLED=true
SCHEDULED_BACKUP_DIR=/app/backups/scheduled
SCHEDULED_BACKUP_STATUS_FILE=/app/backup-status/scheduled-backup.json
SCHEDULED_BACKUP_RETENTION_COUNT=14
SCHEDULED_BACKUP_INCLUDED_GROUPS_JSON=["config_file","runtime_overrides_file","profile_file","mapping_file","sas_fabric_alias_file","slot_detail_file","history_db"]
SCHEDULED_BACKUP_PASSPHRASE_FILE=/run/backup-secrets/scheduled-backup-passphrase
HISTORY_SEGMENTED_BACKUP_MAX_AGE_SECONDS=129600
```

Replace `1000` with the values from `id -u` and `id -g`. Set `APP_GID` to the numeric application group used by the base Compose file.

The backup container keeps its host UID and GID and receives `APP_GID` as a supplemental group. The setgid `2750` status directory makes atomic status-file replacements inherit that group. Status files use `0640`. Archives and the passphrase remain private `0600` files.

The segment directory uses exact mode `0750`; segments and `catalog.json` use exact mode `0640`. Their owner and group match the hot history database.

The non-root application UID owns segment publication. The backup UID reads the files through the `APP_GID` supplemental group and cannot modify them.

Do not run migration, sealing, rotation, or recovery as host root when the hot database belongs to the non-root application UID. The publisher rejects a process whose effective UID does not own the hot database. This prevents a root-owned replacement from making the history service read-only.

### Repair old segmented-history permissions

A deployment with a `0600` catalog or segments needs one bounded permission repair before a separate backup UID can read them.

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

When `history_db` is selected, the runner creates `.7z`. Without `history_db`, it creates a validated inner system backup and encrypts the `.tar.zst.enc` envelope with AES-256-GCM, a per-file salt, and a per-file nonce. Import either format through the admin restore path with the same passphrase.

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
