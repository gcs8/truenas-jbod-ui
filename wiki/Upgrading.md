# Upgrading

Use this page when a new release is out. The main UI header shows
`Update available` with the new version when one exists.

## Before you start

Find the target tag on the
[releases page](https://github.com/gcs8/truenas-jbod-ui/releases) and read its
upgrade notes. Record what you are running now, so you can go back:

```bash
docker compose images
```

Keep a verified backup of `config`, `data` and `history`, your `.env`, and every
Compose file you start the stack with.

## Move to a new release

A normal upgrade changes only the image pin:

1. In the deployment folder, set `JBOD_UI_IMAGE` in `.env` to the new tag or
   digest.
2. Run `docker compose pull` and `docker compose up -d` with the same ordered
   `-f` files and profiles you always use (or keep `COMPOSE_PROFILES` in
   `.env`).
3. Check `docker compose ps` and each enabled service's `/healthz`.

Keep your existing Compose files. Do not use `down` for a normal update, and
do not download a replacement Compose file. The full steps are in
[[Docker and GHCR Deployment|Docker-and-GHCR-Deployment#update]].

## History

A normal upgrade needs nothing: the history service migrates its database when
it starts with the new image, and the next start finishes an interrupted
migration. Two cases need attention before you rely on the data:

- If `/healthz` or the history log reports a recovery it could not finish,
  follow [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
  with your backup instead of deleting files.
- If the log says the database was unreadable and was moved aside, the original
  file is kept; recover it from the same page before new collection builds on
  an empty database.

Segmented history is an advanced opt-in. Ignore its tools unless you set
`HISTORY_SEGMENT_CATALOG_PATH` yourself.

## Admin UI

The published Compose files set `ADMIN_AUTO_STOP_SECONDS=3600`, so the admin UI
stops itself an hour after it starts, and a plain `docker compose up -d` does
not bring it back. Start it again with:

```bash
docker compose --profile admin up -d enclosure-admin
```

## Optional hardening

Adding `docker-compose.nonroot.yml` is a separate change, not part of an image
update. See
[[Docker and GHCR Deployment|Docker-and-GHCR-Deployment#optional-container-hardening]]
and [[Troubleshooting]] and the release upgrade notes for the one-time
ownership step.

## What is tested

Every pull request runs the upgrade below in CI, on a disposable Linux Docker
host with synthetic data. Anything not in this table has not been tested, so
treat it as unverified, not as broken.

| Area | Tested | Not tested yet |
| --- | --- | --- |
| Docker host | Linux, Docker Engine with Compose v2, `linux/amd64` image | Docker Desktop, other architectures, rootless Docker |
| Local state | Bind mounts on a local POSIX filesystem, owned by root | Network filesystems, read-only or full disks |
| Services | Main UI and history on the published base Compose file | Admin and backup services, and the hardening overlay, during an upgrade |
| Upgrade path | v0.22.2 to the current build: change `JBOD_UI_IMAGE`, then `pull` and `up -d` | Releases before v0.22.2, skipping several releases |
| After upgrading | Both containers healthy with no restarts; the new version and revision are reported; bay mappings, history rows and `config.yaml` unchanged; database integrity check passes; history schema migrated at startup; no change of file ownership | Large (multi-GiB) history databases, many enclosures |
| Rollback | Pin back to v0.22.2 with the same two commands; the older release starts and reads what the newer one wrote | Rollback across a history schema change |
| Recovery | | An interrupted migration or restore, an encrypted restore on a clean host |

The check is `scripts/run_image_upgrade_smoke.py`, run by the `Image-only
upgrade smoke` CI job.

## Rolling back a release

Set `JBOD_UI_IMAGE` back to the recorded tag or digest and run the same `pull`
and `up -d`. Durable data is not rolled back with the image; see
[[Rolling back a release|Docker-and-GHCR-Deployment#rolling-back-a-release]]
for what that means for history.
