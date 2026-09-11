# Upgrading

Use this page when a new release is out. The main UI header shows
`Update available` with the new version when one exists.

## Move to a new release

Find the target tag on the
[releases page](https://github.com/gcs8/truenas-jbod-ui/releases). Publication
alone is not upgrade qualification. Before changing the image, save a private
pre-upgrade backup of state, the environment file, all selected Compose files,
and the previous `JBOD_UI_IMAGE` digest or tag. A moving `latest` tag alone is
not a reproducible rollback pin.

Record the existing project name, `--env-file`, ordered `-f` file chain,
`--profile` selections and active service names. Keep them for update and
rollback. Do not add inactive admin or backup services. In the deployment
folder, change only `JBOD_UI_IMAGE` in the selected environment file to the
chosen image pin. Ensure an exported shell value does not override that pin.
Then run `pull` and `up -d` with that exact recorded selection and service list.
For example, this is only for an existing project named `jbod-ui` with a single
`compose.yaml`, `.env`, and UI plus history already active:

```bash
docker compose --project-name jbod-ui --env-file .env -f compose.yaml --profile history pull enclosure-ui enclosure-history
docker compose --project-name jbod-ui --env-file .env -f compose.yaml --profile history up -d enclosure-ui enclosure-history
```

Adapt every selection to the existing deployment, retaining any overlays in
order. Do not use `down` for a normal image update. Reload the UI and verify
service health, version and retained state before resuming collection or jobs.

Keep the existing Compose files for a normal image update. Replacing them with
the v0.23.0 non-root file is a separate migration requiring the bounded
ownership procedure in [[Troubleshooting]], not a prerequisite to an image bump.
The v0.23.0 `scripts/update_immutable_deployment.py` replaces the selected
Compose files from its source revision during update; it is not an image-only
shortcut. Do not assume unmerged helper changes are present in released source.

## History

For a normal upgrade there is nothing to do: the history service upgrades its
database on first start with the new image, and the upgrade is resumable if the
container restarts part-way through.

Two cases need attention before you rely on the data:

- If the history container refuses to start and its log names a pending
  recovery marker, an earlier rotation or restore did not finish. Follow
  [[History Maintenance and Recovery|History-Maintenance-and-Recovery]] with
  your pre-upgrade backup instead of deleting files.
- If the log says the existing database was unreadable and was moved aside,
  the service starts empty. The original file is kept next to it; recover it
  from the same page before collection overwrites your history.

These steps keep the upgrade moving. They do not establish that every older
schema is accepted by every newer image; that qualification is tracked
separately in #416 and #417.

Segmented history is an advanced opt-in. Ignore the segmented-history pages
and tools unless you set `HISTORY_SEGMENT_CATALOG_PATH` yourself.

## Admin UI

The admin UI stops itself one hour after it starts; the published Compose files
set `ADMIN_AUTO_STOP_SECONDS=3600`. A plain `docker compose up -d` does not
bring it back. Start it again with:

```bash
docker compose --profile admin up -d enclosure-admin
```

## Rolling back a release

1. Stop writers before any state recovery. Restore `JBOD_UI_IMAGE` to the
   recorded previous digest or tag in the selected environment file; ensure
   shell interpolation does not override it.
2. Restore every Compose file changed during migration. Retain the recorded
   `--project-name`, `--env-file`, ordered `-f` file chain, `--profile`
   selections and previous service list for both `pull` and `up -d`. Use the
   same command shape as the update example with your actual prior selections.
   Bare commands may select a different file or omit optional services.
3. Check service health and retained state before restarting collection or jobs.

This restores runtime selection, not data-format compatibility. History schema
changes may prevent an older image from using newer state. If that occurs, keep
writers stopped and follow the recovery procedure for the selected version with
the private pre-upgrade backup. Do not delete history or assume an image pin
restores data. These instructions do not establish predecessor upgrade,
retention or rollback qualification for #399/#400.
