# History and Snapshot Export

This page is the visual guide for the optional history sidecar and the offline
snapshot export flow.

Use it when you want historical charts, timeline-backed heat maps, or one
self-contained offline HTML view of an enclosure. If you are trying to choose
between snapshot export, debug bundles, full backups, and the public demo, start
with
[[Demo and Offline Workflows|Demo-and-Offline-Workflows]].

## What This Adds

When the optional history sidecar is running, the main UI can:

- show a `History` button in Slot Details
- open a wide slot-history drawer under the enclosure
- render temperature and read/write history in browser-local time
- provide sampled data for [[Heat Map Mode|Heat-Map-Mode]] timelines and
  history-backed heat-map metrics
- export a frozen offline HTML snapshot of the current enclosure

The history sidecar is optional, but it is a normal supported runtime service,
not a dev-only helper. If it is unavailable, the live app keeps working and
snapshot exports still work, but they omit historical samples and events.

## Request And Refresh Boundaries

Bulk history reads require an explicit window from 1 hour through 1 year. The
service accepts only the six documented history metrics and owns aggregate
ceilings of 32 scopes, 347 scope-slot targets, 4,096 event rows, 36,000 total
projected rows, a 64 KiB multi-scope request, and a 24 MiB serialized response.
Heat-map playback requests events at zero, only the one or two selected metrics,
and at most 24 samples per metric. The single-slot drawer keeps its separately
bounded behavior. An unavailable optional sidecar still degrades to unavailable
history rather than breaking inventory views or exports.

The published history port remains loopback-only by default. If you intentionally
publish it on a non-loopback address, set `HISTORY_REFRESH_AUTH_MODE=token`,
configure the exact `HISTORY_PUBLIC_ORIGIN`, and provide
`HISTORY_REFRESH_TOKEN_FILE` through the secrets overlay (or a privately
protected `HISTORY_REFRESH_TOKEN`). Direct refresh requests use a strict JSON
`{"mode":"full"}` and an `Authorization: Bearer ***` header. Never
put that token in a browser URL or page. Browser operators should use the
Basic-authenticated main-UI refresh proxy instead.

Full refresh admission has a 900-second server-owned cooldown by default.
Concurrent refreshes return `409`; a full refresh inside the cooldown returns
`429` with `Retry-After`. Failed full-refresh attempts also start the cooldown,
and a process restart resets this in-memory timestamp.

## Start The Optional History Sidecar

Use the same folder you created in [[Quick Start|Quick-Start]], where
`compose.yaml` and `.env` live:

```bash
docker compose --profile history pull
docker compose --profile history up -d
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

Long-horizon deployments can use one writable hot database plus immutable
segments. Configure it only after the offline migration publishes a complete
catalog:

```dotenv
HISTORY_SQLITE_PATH=/app/history/history.db
HISTORY_SEGMENT_CATALOG_PATH=/app/history/segments/catalog.json
```

Mount the whole history directory into every history/admin/backup container.
Do not bind-mount only the SQLite file. If the catalog is missing, invalid,
digest-mismatched, or recovery-pending, history reads fail visibly instead of
falling back to hot-only data.

Segmented queries merge slot events, raw/hourly/daily metrics, disk-followed
history, scope history, counts, and summaries across databases. They do not use
SQLite `ATTACH`. A query selects at most 32 segments and 5,000 rows; broader
requests fail rather than silently omitting older history.

Migration, recovery, and rollback commands are documented in
[Segmented history v2](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/SEGMENTED_HISTORY_V2.md).

The recovery tools packaged by the current `main` source-build image are:

- `/app/scripts/migrate_segmented_history.py`
- `/app/scripts/rotate_segmented_history.py`
- `/app/scripts/query_segmented_history.py`
- `/app/scripts/seal_history_segment.py`

The v0.22.2 published image does not contain these tools. Do not run the
commands in the current-main runbook against that older image. Build the
matching current source and use its dev Compose file until a release that
contains the tools is published.

## History Sidecar Dashboard

Open the history sidecar directly when you want to see what the collector is
doing:

```text
http://your-docker-host:8081
```

The dashboard now updates without a browser refresh:

- the collector status block follows cheap `/healthz` polling
- count cards, DB size, and tracked scopes follow the overview poll
- `Refresh Fast` runs the cached-root-only path for the current root scope
- `Refresh Full` runs the slower forced-inventory path and records stage
  timings so you can see where the time went

On a cold cache, a fast refresh may record a bounded `smart.failed` stage after
roughly five seconds. That means the cached SMART batch was unavailable or
slow; it should not set `last_error` or leave the collector stuck.

## What The Live History Drawer Looks Like

Once the sidecar is healthy, pick a populated slot and use the `History`
button in Slot Details.


Things to notice:

- the drawer opens under the enclosure instead of stretching the right detail rail
- the window picker applies to the whole history pane
- the read/write chart supports both total and average views
- recent events stay in the same place as you move between slots

The same history drawer is also available for inventory-bound
storage views such as `Boot SATADOMs` and the internal NVMe carrier:


Things to notice:

- disk-oriented metrics can now auto-follow the same physical disk across
  homes when the sidecar has a strong disk identity for it
- slot-change events still stay local to the slot you opened, so the drawer
  does not lie about where a swap or move happened
- if you are renaming or deleting whole systems, use
  [[History Maintenance and Recovery|History-Maintenance-and-Recovery]] for the
  cleanup/adoption tools instead of trying to hand-edit the SQLite DB

## Heat Map Timelines

Heat map mode has its own feature guide now:
[[Heat Map Mode|Heat-Map-Mode]].

The short version: history-backed heat-map metrics can switch from `Current`
to `Timeline`, start on the newest available sample, and scrub backward through
the selected window. After clicking the sample slider, use the left and right
arrow keys for fine one-sample steps.

## What The Snapshot Export Dialog Looks Like

Use `Export Snapshot` from the main toolbar.


Things to notice:

- live size estimates for `HTML`, `ZIP`, and the current choice
- redaction and packaging controls before download
- a clear note that snapshot history uses the current History drawer window
- downsampling feedback if larger exports need rollups later
- packaging changes reuse the estimate already on screen instead of
  recalculating the whole export payload

If you want a different snapshot history range, change the window in the
History drawer first, then open the export dialog.

### What Redact sensitive IDs changes

When `Redact sensitive IDs` is on, the exporter gives related
system and enclosure names and IDs stable aliases such as `host-01` and
`enc-01`. It masks
serial values to a short suffix and partially masks WWN, SAS, NAA, UUID, GPTID,
LUN, transport, NGUID, and EUI-64 identifiers. It also masks IPv4 values while
keeping the last octet and masks canonical, uncompressed IPv6 values while
keeping the final groups. Configured hostnames are replaced wherever the
exporter recognizes them.

The current matcher does not cover every free-form value. In particular,
compressed IPv6 addresses require manual review. The toggle leaves model,
firmware, capacity, health, metrics, pool names, and other unclassified text in
the artifact. Review the exported file before sharing it; the toggle is a
bounded identifier scrub, not a promise that arbitrary text contains no private
data.

## What The Offline Snapshot Looks Like

The export produces a self-contained HTML file that opens locally without
access to the live app.


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
- the [[Public Demo Site|Public-Demo-Site]] is a static sample data
  experience, not a backup/restore path and not a live hosted backend

Use the debug bundle when you want to hand someone a frozen stack state to
inspect. Use full backup when you actually need to restore the app later.

The debug bundle is not a standalone HTML viewer and it is not an import path
today. It does, however, support separate `Scrub obvious secrets` and `Scrub
disk identifiers` toggles so you can choose how much local detail to share.

The full backup/debug-bundle details live in
[[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].

## Advanced Source Builds

Most users should use the published-image commands above. Use the source-build
command only when you are editing the app, testing an unmerged branch, or
intentionally rebuilding the image on that machine.

From a cloned repo:

```bash
docker compose -f docker-compose.dev.yml --profile history up -d --build
```

## If History Is Unavailable

The app should degrade like this:

- the `History` button stays hidden
- the export dialog warns that history will be omitted
- snapshot estimate and export still work
- the exported snapshot opens without historical charts or events

## Related Pages

- [[Visual Tour|Visual-Tour]]
- [[Heat Map Mode|Heat-Map-Mode]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
