# History and Snapshot Export

This page is the visual guide for the optional history sidecar and the offline
snapshot export flow.

## What This Adds

When the optional history sidecar is running, the main UI can:

- show a `History` button in Slot Details
- open a wide slot-history drawer under the enclosure
- render temperature and read/write history in browser-local time
- export a frozen offline HTML snapshot of the current enclosure

The history sidecar is optional, but it is a normal supported runtime service,
not a dev-only helper. If it is unavailable, the live app keeps working and
snapshot exports still work, but they omit historical samples and events.

## Start The Optional History Sidecar

From the repo root, start it from the default published-image path:

```bash
docker compose --profile history up -d
```

If you are intentionally building from source instead:

```bash
docker compose -f docker-compose.dev.yml --profile history up -d --build
```

That keeps the main UI on `:8080` and starts the small history collector/API
sidecar on `127.0.0.1:8081`.

By default it also keeps:

- the live SQLite DB at `./history/history.db`
- short-term rotating backups at `./history/backups`
- weekly and monthly promoted long-term backups at
  `./history/backups/long-term`

If you later mount a separate disk or NFS path for longer-lived copies, point
`HISTORY_LONG_TERM_BACKUP_DIR` there and keep the short-term local path in
place.

## What The Live History Drawer Looks Like

Once the sidecar is healthy, pick a populated slot and use the `History`
button in Slot Details.

![Live slot history drawer](images/history-drawer-v0.14.0.png)

Things to notice:

- the drawer opens under the enclosure instead of stretching the right detail rail
- the window picker applies to the whole history pane
- the read/write chart supports both total and average views
- recent events stay in the same place as you move between slots

The same history drawer is also available for inventory-bound
storage views such as `Boot SATADOMs` and the internal NVMe carrier:

![Storage-view history on Boot SATADOMs](images/storage-view-history-v0.14.0.png)

Things to notice:

- disk-oriented metrics can now auto-follow the same physical disk across
  homes when the sidecar has a strong disk identity for it
- slot-change events still stay local to the slot you opened, so the drawer
  does not lie about where a swap or move happened
- if you are renaming or deleting whole systems, use
  [[History Maintenance and Recovery|History-Maintenance-and-Recovery]] for the
  cleanup/adoption tools instead of trying to hand-edit the SQLite DB

## What The Snapshot Export Dialog Looks Like

Use `Export Snapshot` from the main toolbar.

![Snapshot export dialog with live estimate](images/snapshot-export-dialog-v0.14.0.png)

Things to notice:

- live size estimates for `HTML`, `ZIP`, and the current choice
- redaction and packaging controls before download
- a clear note that snapshot history uses the current History drawer window
- downsampling feedback if larger exports need rollups later

If you want a different snapshot history range, change the window in the
History drawer first, then open the export dialog.

## What The Offline Snapshot Looks Like

The export produces a self-contained HTML file that opens locally without
access to the live app.

![Frozen offline enclosure snapshot](images/offline-snapshot-v0.14.0.png)

Things to notice:

- the `Frozen Offline Artifact` banner makes it clear this is not the live UI
- the selected slot can stay selected in the snapshot
- the history drawer can stay open if it was open when exported
- live actions stay disabled, but slot inspection and navigation still work

## Snapshot Export Versus Admin Debug Bundle

These are intentionally different tools:

- `Export Snapshot` in the main UI creates one self-contained offline HTML
  artifact for the currently selected enclosure or storage view
- `Debug Bundle` in the admin sidecar on `:8082` creates a normal archive with
  selected config/history/support files for offline troubleshooting
- `Full Backup` in the admin sidecar creates the restore-grade bundle you use
  for import or cross-host recovery

Use the debug bundle when you want to hand someone a frozen stack state to
inspect. Use full backup when you actually need to restore the app later.

The debug bundle is not a standalone HTML viewer and it is not an import path
today. It does, however, support separate `Scrub obvious secrets` and `Scrub
disk identifiers` toggles so you can choose how much local detail to share.

## If History Is Unavailable

The app should degrade like this:

- the `History` button stays hidden
- the export dialog warns that history will be omitted
- snapshot estimate and export still work
- the exported snapshot opens without historical charts or events
