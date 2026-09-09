# History and snapshot export

Run the optional history sidecar to collect historical metrics and events. Use snapshot export to save the selected enclosure or storage view as a self-contained HTML file.

For a comparison with debug bundles, full backups, and the public demo, see [[Demo and Offline Workflows|Demo-and-Offline-Workflows]].

## Start the history sidecar

Run these commands from the deployment directory that contains `compose.yaml` and `.env`:

```bash
docker compose --profile history pull
docker compose --profile history up -d
```

The main UI remains on port `8080`. The history collector and API listen on `127.0.0.1:8081` by default.

The default paths are:

- live SQLite database: `./history/history.db`
- rotating backups: `./history/backups`
- weekly and monthly backups: `./history/backups/long-term`

To store long-term backups on another disk or NFS path, set `HISTORY_LONG_TERM_BACKUP_DIR` to that location and keep the short-term local backup path.

## Use history in the main UI

When the history sidecar is available, the main UI provides:

- a `History` button in Slot Details
- a slot-history drawer below the enclosure
- temperature and read/write charts in browser-local time
- history-backed heat-map metrics and timeline playback
- frozen history samples in snapshot exports when selected

Select a populated slot and click `History`. The window selector applies to the full drawer. Read/write charts support total and average views, and recent events remain visible when you move between slots.

Inventory-bound storage views, including boot devices and internal NVMe carriers, use the same drawer. Disk-oriented metrics can follow a physical disk between locations when the service has a strong disk identity. Slot-change events remain attached to the slot where they occurred.

If the sidecar is unavailable, the `History` button is hidden. The main UI and snapshot export continue to work, but exports omit historical samples and events.

## History request limits

Bulk history requests must specify a window from 1 hour through 1 year. The API accepts the six documented history metrics and applies these limits:

- 32 scopes
- 347 scope-slot targets
- 4,096 event rows
- 36,000 projected rows across the request
- 64 KiB request body for multi-scope requests
- 24 MiB serialized response

Heat-map playback requests no events, requests only the selected one or two metrics, and returns at most 24 samples per metric. The single-slot drawer uses its own bounded request path.

## Refresh the collector

Open the history dashboard at:

```text
http://your-docker-host:8081
```

The dashboard updates collector health, database size, tracked scopes, and counts without a browser reload.

- `Refresh Fast` refreshes the current root scope from cached inventory.
- `Refresh Full` forces inventory collection and records stage timings.

A cold-cache fast refresh can record a bounded `smart.failed` stage after about five seconds if the cached SMART batch is unavailable or slow. This does not set `last_error` or leave the collector stuck.

Full refresh has a server-owned cooldown of 900 seconds by default. Concurrent refreshes return `409`. A refresh during the cooldown returns `429` with `Retry-After`. Failed full-refresh attempts also start the cooldown. Restarting the history process resets the in-memory cooldown timestamp.

### Publish history off-host

The history service listens on loopback by default. If you intentionally bind it to a non-loopback address, configure refresh protection:

```dotenv
HISTORY_REFRESH_AUTH_MODE=token
HISTORY_PUBLIC_ORIGIN=https://history.example.test
```

Set `HISTORY_PUBLIC_ORIGIN` to the exact browser origin. Set `HISTORY_REFRESH_TOKEN_FILE` to the token file mounted through the secrets overlay, such as `/run/secrets/history_refresh_token`, or use a privately protected `HISTORY_REFRESH_TOKEN`.

Direct refresh requests must send the JSON body `{"mode":"full"}` and a bearer-token authorization header. Never place the token in a URL or web page. Browser operators should refresh through the Basic-authenticated main-UI proxy.

## Configure segmented history

Long-horizon deployments can keep one writable database plus immutable segments. Complete the offline migration and publish a valid catalog before enabling these settings:

```dotenv
HISTORY_SQLITE_PATH=/app/history/history.db
HISTORY_SEGMENT_CATALOG_PATH=/app/history/segments/catalog.json
```

Mount the entire history directory in each history, admin, and backup container. Do not bind-mount only the SQLite file.

History reads fail visibly when the catalog is missing, invalid, digest-mismatched, or marked for recovery. They do not fall back to hot-only data.

Segmented queries merge slot events, raw, hourly, and daily metrics, disk-followed history, scope history, counts, and summaries without SQLite `ATTACH`. Each query can select up to 32 segments and return up to 5,000 rows. Broader requests fail instead of silently omitting older data.

Before running segmented maintenance commands, confirm the container contains these tools:

- `/app/scripts/migrate_segmented_history.py`
- `/app/scripts/rotate_segmented_history.py`
- `/app/scripts/query_segmented_history.py`
- `/app/scripts/seal_history_segment.py`

Images before v0.23.0 do not contain these tools. Use an image that contains all four paths before running segmented-history commands.

Use the commands and version checks in [Segmented history v2](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/SEGMENTED_HISTORY_V2.md).

## Use heat-map timelines

Choose a history-backed metric in [[Heat Map Mode|Heat-Map-Mode]], set a window, and switch `Mode` from `Current` to `Timeline`. Timeline mode starts at the newest available sample. Drag the slider to move through samples. After focusing the slider, use the left and right arrow keys to move one sample at a time.

## Export a snapshot

Select an enclosure or storage view, then choose `Export Snapshot` from the main toolbar.

The dialog shows estimated sizes for the HTML, ZIP, and selected package. Snapshot history uses the window currently selected in the History drawer. Change that window before opening the export dialog if you need a different range.

The export is a self-contained HTML file. It opens without access to the live application and displays a `Frozen Offline Artifact` banner. The selected slot and an open history drawer can remain selected in the file. Inspection and navigation work, but live actions remain disabled.

### Redact sensitive IDs

Enable `Redact sensitive IDs` to apply stable aliases such as `host-01` and `enc-01` to recognized system and enclosure names and IDs. The exporter also:

- masks serial values to a short suffix
- partially masks WWN, SAS, NAA, UUID, GPTID, LUN, transport, NGUID, and EUI-64 identifiers
- masks IPv4 addresses while retaining the last octet
- masks canonical, uncompressed IPv6 addresses while retaining the final groups
- replaces configured hostnames where it recognizes them

This is a bounded identifier scrub, not a complete privacy filter. Compressed IPv6 addresses and arbitrary free-form values may remain. The model, firmware, capacity, health, metrics, pool names, and unclassified text remain in the artifact. Review the exported file before sharing it.

## Choose snapshot, debug bundle, or backup

- `Export Snapshot` creates an offline HTML view of the selected enclosure or storage view.
- `Debug Bundle` creates a non-importable support archive from selected local files.
- `Full Backup` creates a restore-grade archive for migration or recovery.
- The [[Public Demo Site|Public-Demo-Site]] contains synthetic sample data and is not a backup or live backend.

See [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]] for archive and restore instructions.

## Related pages

- [[Visual Tour|Visual-Tour]]
- [[Heat Map Mode|Heat-Map-Mode]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
