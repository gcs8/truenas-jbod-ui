# Upgrading

Use this page when a new release is out. The main UI header shows
`Update available` with the new version when one exists.

## Move to a new release

Find the new tag on the
[releases page](https://github.com/gcs8/truenas-jbod-ui/releases); the newest
one is marked Latest. Then, in the folder where `compose.yaml` and `.env` live:

1. Change the tag at the end of the `JBOD_UI_IMAGE` line in `.env` to the new
   one, for example `v0.23.0`. Skip this step if the line ends in `latest`.
2. Pull the new image and recreate the containers.
3. Reload the main UI and check the version shown in the header.

```bash
docker compose pull
docker compose up -d
```

Keep your existing Compose file for a normal image update. Replace it only when
a release says so; the v0.23.0 Compose file runs the main UI and history as a
non-root user and needs the ownership step in [[Troubleshooting]] before its
first start.

If you run history without `COMPOSE_PROFILES=history` in `.env`, add
`--profile history` to both commands so history is updated too.

## History

Nothing to do. The history service upgrades its database on first start with
the new image.

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

1. Set `JBOD_UI_IMAGE` in `.env` back to the previous tag.
2. Put the previous Compose file back if you replaced it during the upgrade.
3. Run `docker compose pull` and `docker compose up -d` again.

History database changes are forward-only. If the older release does not start
cleanly against the newer database, restore a backup of the `history` folder
taken before the upgrade. Bay assignments in `data` and settings in `config`
are not changed by an image update.
