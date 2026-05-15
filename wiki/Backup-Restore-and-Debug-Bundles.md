# Backup, Restore, and Debug Bundles

This page explains the export and recovery tools that live in the optional
admin sidecar.

The short version:

- use `Full Backup` when you may need to restore or move the local app state
- use `Debug Bundle` when you want a support artifact for inspection
- use `Export Snapshot` when you want one offline HTML enclosure view
- use history purge/adopt tools only after exporting anything you might care
  about later

Open the admin sidecar on `:8082` for these workflows.

![Admin maintenance bundle and history tools](images/admin-maintenance-v0.17.0.png)

## Tool Picker

| Tool | Output | Importable? | Best for |
| --- | --- | --- | --- |
| `Full Backup` | restore-grade archive | yes | migration, disaster recovery, release-candidate restore drills |
| `Debug Bundle` | support archive | no | sharing scrubbed config/history/log evidence for review |
| `Export Snapshot` | self-contained HTML file | no | sharing the current enclosure view offline |
| `Purge Orphaned Data` | maintenance action | not an export | cleaning deleted/renamed system history |
| `Adopt Removed System History` | maintenance action | not an export | rehoming old history into a current saved system id |

## Full Backup Bundles

Use a full backup when you may want to restore the app state later.

The default plaintext scope covers core state:

- `config/config.yaml`
- `config/profiles.yaml`
- slot mappings and slot-detail cache JSON
- the history SQLite database

The secret-material paths are locked because they can contain credentials or
trust roots:

- `config/ssh`
- imported TLS trust bundles
- shared `known_hosts`

Selecting any locked path forces encrypted portable `.7z` export. That keeps
secret material out of plaintext bundles while still letting the admin import
path restore those same selected files later.

## Restore Pattern

For real migrations:

1. export a full backup from the source stack
2. start the target Docker stack with separate local folders
3. import the bundle through the admin sidecar
4. restart the main UI and sidecars
5. verify `/livez`, the runtime selector, one live enclosure, and one history
   drawer or history dashboard view

For release-candidate or destructive testing, use a disposable QA stack with
separate ports and separate runtime folders. Do not run import, restore, purge,
adopt, delete, or runtime override tests against the long-running production
stack unless you explicitly intend to change it.

## Debug Bundles

The `Debug Bundle` card is different from full backup.

Use it when you want a frozen support snapshot of local state for offline
inspection. It:

- exports a normal archive, not a self-contained HTML viewer
- is not an importable restore path
- can stop/restart the UI and history sidecar around capture
- has separate `Scrub obvious secrets` and `Scrub disk identifiers` toggles

If `Scrub obvious secrets` stays on, the locked secret-path pills remain
disabled so private keys and trust material do not accidentally ride along.

## Snapshot Export Is Separate

`Export Snapshot` in the main UI creates a single self-contained HTML artifact
for the current enclosure or storage view.

That is useful when you want someone to inspect a physical slot map without
connecting to the live app. It is not a restore path and does not carry the full
local stack state.

See [[History and Snapshot Export|History-and-Snapshot-Export]].

## History Cleanup Safety

Before deleting or adopting history rows:

1. export a full backup if the rows may matter later
2. confirm the target saved system id
3. use preview or low-risk cleanup paths first when available
4. verify the history drawer or history dashboard afterward

The history-specific cleanup guide lives at
[[History Maintenance and Recovery|History-Maintenance-and-Recovery]].

## Related Pages

- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Architecture and Services|Architecture-and-Services]]
