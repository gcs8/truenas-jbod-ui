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
