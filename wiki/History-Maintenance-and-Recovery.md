# History maintenance and recovery

Use the admin maintenance tools when a saved system is deleted, renamed, or rebuilt under a new `system_id`. Do not edit the SQLite database by hand.

## Back up history before changing it

Create a `Full Backup` in the admin sidecar before any destructive cleanup. Include the history database and any configuration, profiles, mappings, or cache files needed for recovery.

If you are unsure whether to purge or adopt history, stop and export the backup first.

## Choose a delete action

The `Existing Systems` panel provides two delete actions.

### Delete System

`Delete System` removes the saved configuration entry and leaves matching history rows in place. Use it when you plan to recreate the system with the same ID or adopt its history into another saved system.

If you recreate the system with the same `system_id`, the old history continues to match it.

### Delete + Purge History

`Delete + Purge History` removes the saved configuration entry and deletes matching history rows. Use it only when you want a clean start and no longer need that history.

Segmented history operations fail closed because these delete, purge, and adopt actions do not rewrite immutable segments. Use the version-checked recovery tools in [Segmented history v2](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/SEGMENTED_HISTORY_V2.md) and publish a complete replacement catalog generation.

## Purge orphaned data

Use `Purge Orphaned Data` when a system no longer exists in `config.yaml` and you no longer need its history.

The action removes rows only for `system_id` values that are absent from the saved configuration. It does not remove rows for active saved systems.

## Adopt removed system history

Use `Adopt Removed System History` after renaming a saved system or recreating the same appliance under a new ID.

1. Select one orphaned source `system_id`.
2. Select one current saved target `system_id`.
3. Run the adoption.
4. Verify the target in the history drawer or history dashboard.

Adoption rewrites history ownership for the whole source system ID. Use it for a change such as `old-system-id` to `replacement-system-id`, not for an individual disk move.

Disk-oriented metrics can already follow a physical disk between locations when the read path has a strong disk identity. Slot events remain local to the slot where they occurred. Use adoption when the saved system ID itself changed.

## Leftover staging artifact

Segmented recovery can report unexpected files named like `.<hot-name>.segmented-*.sqlite3`, `.rotation-catalog-*.json`, or `.segment-*.sqlite3`. Migration recovery reports every unreferenced `.segment-*.sqlite3` temporary artifact. A matching filename does not prove the migration or rotation process owns the file.

1. Stop or otherwise quiesce the history service and every history writer or maintenance job.
2. Record the exact path and metadata for each reported file.
3. Inspect the pending activation journal and active `catalog.json`.
4. Prove that the reported file is not journal-referenced and not catalog-selected.
5. Remove only the named path after proving it is unreferenced.
6. Rerun dry-run recovery without `--apply`.
7. Review the proposed action before applying recovery.

Never use wildcard deletion in the history directory. Leave every other hot database, catalog, staging file, rollback file, journal, and segment untouched. If ownership or catalog membership is unclear, keep the file and stop. Filename, age, and parseable contents are not proof of ownership.

## Read the history dashboard diagnostics

The history dashboard reports failures as plain sentences. The raw exception
text, which can carry URLs, file paths, and appliance replies, stays in the
service logs.

| Cell | What it means |
| --- | --- |
| `Last error` | A collection pass failed. The text stays generic on purpose. |
| `What went wrong` | The classified reason for that failure, such as not reaching the main UI, a request timeout, or a rejected request with its status code. |
| `Backup error` | The last snapshot attempt failed, named in plain words: a full disk, an unwritable backup directory, or a read-only database. |
| `Cleanup error` | The last retention pass failed, named in plain words (read-only database, full disk, missing permission). Retention selects each batch with a bounded subquery, so any `HISTORY_RETENTION_BATCH_SIZE` works; a very large value only makes each cleanup transaction longer. |
| `Cleanup waiting` | Retention is holding off because no recent backup exists. |
| `Cleanup resumes by` | The deadline after which retention prunes anyway. |
| `Full refresh available` | When the next manual full refresh is allowed, instead of a refusal after the fact. |

The history service `/healthz` answers HTTP 503 with `status: down` only when
the history database could not be opened at startup (for example an unwritable
`./history` folder); `reason` carries the plain line from the log. Otherwise it
answers HTTP 200. Its `status` is `degraded`, with a plain `detail`, when the last background
collection failed, the history database is read-only, cleanup failed twice in a
row, or earlier history was quarantined and needs recovery. A failed manual
refresh shows in `Last error` but does not make the service degraded. The
Collector card reads `Starting` during the startup grace period, and a scheduled
backup status file with group or world write permission is named in
`Cleanup error` (expected mode `0640`).

If `Cleanup waiting` stays set, fix the backup first: read `Backup error`, check
that the backup directory is writable by the history service and has free
space, then confirm that `Last backup` moves forward. Pruning resumes on its own
once a snapshot succeeds, and no later than `Cleanup resumes by`.

`Cleanup resumes by` is a real deadline across restarts: the moment the wait
started is written into the history database, so restarting the service does
not push it out and does not bring it forward. If that record cannot be read or
written, `Cleanup waiting` says the retention wait record could not be read or
written and nothing is pruned at all until the database is writable again.

## Recover a damaged history database

If SQLite reports the history database as damaged while the service is running
(`database disk image is malformed` or `file is not a database`), collection
stops writing to protect what is left. `/healthz` reports `degraded` with "The
history database is damaged; collection is paused to protect it." The
dashboard shows `Collection paused: yes`, and a manual refresh is refused. Reads
stay available. The pause is recorded in a small marker file next to the
database (`history.sqlite3.collection-paused`, holding only a timestamp), so a
restart does not resume writing.

The same state is used when startup had to quarantine an unreadable database
and start an empty one (`Recovery required: yes`). The original file is kept
next to the database as `*.broken-*` and is never deleted automatically.

To recover:

1. Restore a full backup that includes the history database from the admin
   **Backups** page, or put a known-good copy of the database in place.
2. Check the result:

   ```bash
   docker compose exec enclosure-history python -m history_service.recovery status
   ```

   `database_check` must read `ok`.
3. Acknowledge the recovery:

   ```bash
   docker compose exec enclosure-history python -m history_service.recovery acknowledge
   ```

   This refuses and changes nothing while the integrity check fails. When the
   check passes, it removes the pause marker and acknowledges a pending
   quarantine. Collection resumes on the next pass. It does not touch
   `*.broken-*` files or history rows, and it adds no network endpoint.

## Roll back after an incompatible history change

So far every history schema change only adds to the database, and the previous
release can still open an upgraded one. A future release may make a change the
previous release cannot read. Its upgrade notes will say so. Releases after
v0.23.0 refuse to start history on such a database, name the newer schema
version and do not write to the file. v0.23.0 and older have no such check, so
never start them on the newer database.

There are no down-migrations. Put the pre-upgrade backup back before anything
built from the newer release can open it again, then pin back.

From a backup kept on the admin **Backups** page:

1. Leave the admin UI and the backup scheduler running. They serve that page
   and the backup file.
2. Open **Restore** on the pre-upgrade copy. Keep **Pause the main UI and
   history while importing** checked, and **uncheck Start them again
   afterwards**. Otherwise the newer history service reopens the restored
   database and upgrades it again.
3. Set `JBOD_UI_IMAGE` in `.env` back to the release you were on.
4. Run `docker compose pull` and `docker compose up -d` with your usual files
   and profiles, then check history's `/healthz`.

From a copy of the folders:

1. Stop the stack with `docker compose down`, using your usual files and
   profiles.
2. Put the copied `history` folder (and `config` and `data`, if you copied
   them) back in place.
3. Set `JBOD_UI_IMAGE` back, then `pull` and `up -d` as above.

Everything history recorded after the upgrade is lost: samples, slot events
and any maintenance you did in the newer release. That is the accepted cost of
rolling back across such a change. Take the backup before upgrading; see
[[Upgrading|Upgrading#before-you-start]].

## Common procedures

### Rename a system

1. Save the replacement system under the new `system_id`.
2. Delete the old system with `Delete System`. Do not purge its history.
3. Run `Adopt Removed System History` from the old ID to the new ID.
4. Verify the target system's history.

### Start with no old history

1. Export a full backup if you may need the data later.
2. Select `Delete + Purge History` for the saved system.
3. Add the system again.
4. Verify that its new history starts cleanly.

### Remove history from deleted test systems

1. Delete the stale saved system entries without purging.
2. Open `Purge Orphaned Data`.
3. Confirm that only the intended orphaned IDs appear.
4. Run the purge and verify current systems still have history.

## Related pages

- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Troubleshooting]]
