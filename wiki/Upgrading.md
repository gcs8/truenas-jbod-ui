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

Take a backup right before you upgrade, and keep it until you are sure you will
not roll back. It is the only way back if a release changes the history
database in a way the older release cannot read (see
[[Roll back after an incompatible history change|History-Maintenance-and-Recovery#roll-back-after-an-incompatible-history-change]]).
Either:

- if you run the backup scheduler, open **Backups** in the admin UI, press
  **Back up now** on the full backup, then **Keep** on the new copy so cleanup
  never deletes it; or
- stop the stack and copy `config`, `data` and `history`.

Also keep your `.env` and every Compose file you start the stack with. The
history service's own daily copy under `./history/backups` can be up to a day
old, so do not rely on it alone.

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

If you still start history with the v0.22.2 Compose file and set
`HISTORY_BIND_ADDRESS` to anything other than `127.0.0.1`, add the small
`docker-compose.history-bind.yml` overlay described in the
[history Compose migration](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/HISTORY_COMPOSE_MIGRATION.md).
The old file does not tell the newer history service which address it is
published on, and an image-only update cannot fix that.

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

On current `main`, the base Compose file is root-compatible. Adding the optional
`docker-compose.nonroot.yml` overlay is a separate change, not part of an image
update. See
[[Docker and GHCR Deployment|Docker-and-GHCR-Deployment#optional-container-hardening]]
and [[Troubleshooting]] for the one-time ownership step.

Unlike the `v0.23.0` tag, this is a post-release Compose migration. The v0.23.0
base `docker-compose.yml` already ran the UI and history services non-root with
read-only image filesystems, so that tagged base file required ownership
preparation even without the overlay. Keep the Compose files that belong to the
release you installed; do not apply current `main` defaults retroactively to a
tagged deployment.

## What is tested

Every pull request runs the upgrades below in CI, on a disposable Linux Docker
host with synthetic data. Anything not in this table has not been tested, so
treat it as unverified, not as broken.

| Area | Tested | Not tested yet |
| --- | --- | --- |
| Docker host | Linux, Docker Engine with Compose v2, `linux/amd64` image | Docker Desktop, other architectures, rootless Docker |
| Local state | Bind mounts on a local POSIX filesystem, owned by root; or, with the hardening overlay, handed to uid 10001 by the one-time ownership step in [[Troubleshooting]] | Network filesystems, read-only or full disks |
| Services | Main UI and history on the published base Compose file, and on the base file plus `docker-compose.nonroot.yml` | Admin and backup services during an upgrade |
| Upgrade path | v0.22.2 to the current build: change `JBOD_UI_IMAGE`, then `pull` and `up -d`, with the same `-f` files each time | Releases before v0.22.2, skipping several releases |
| After upgrading | Both containers healthy with no restarts; the new version and revision are reported; bay mappings, history rows and `config.yaml` unchanged; database integrity and schema compatibility checks pass; the report says `schema_transition=none` because v0.22.2 and the candidate use the same schema; a v0.22.2 segmented catalog keeps the exact generation, catalog bytes and referenced segment bytes; no change of file ownership, and hardened services still run as uid 10001 | Large (multi-GiB) history databases, many enclosures |
| Rollback | Pin back to v0.22.2 with the same two commands, on base, hardened and segmented-history deployments; the older release starts and reads what the newer one wrote | Rollback across a history schema change |
| Recovery | The new history container killed (`SIGKILL`) inside each schema-initialization step on v0.22.2 data, one after another; the next `up -d` comes up healthy with no restarts, integrity check `ok`, no rows lost, nothing quarantined, and the same database an uninterrupted start produces | A killed restore, a killed data backfill at runtime (v0.22.2 data needs none; see below), an encrypted restore on a clean host, a full disk |

The checks are `scripts/run_image_upgrade_smoke.py`: the base upgrade runs in
the `Image-only upgrade smoke` CI job, and `--scenario hardened`,
`--scenario interrupted-migration` and `--scenario segmented-catalog` run as
separate steps of the `Hardened, interrupted and segmented upgrade smoke` job.
The segmented run uses v0.22.2's own migration module and reader, captures the
catalog generation plus exact catalog and segment digests, and requires the
same identity and combined history view after upgrade and rollback. v0.22.2
already has the current history schema, so the image-upgrade run proves that
the candidate accepts and preserves it.
It does not exercise a schema transition. The interrupted run kills each
migration-capable startup seam while it is idempotent on this input; the next
start must open the same database as an uninterrupted start.

Separately, `tests/test_history_released_schema_upgrades.py` upgrades synthetic
history databases built from the exact released schemas of v0.8.0, v0.21.2 and
v0.22.2 (v0.23.0 has the same schema as v0.22.2). The tests check row contents,
disk identity keys, the row counters and the SQLite integrity check. They kill
the upgrade after each startup migration step and prove the next start
finishes it. They also refuse a database stamped newer than this build without
changing it. Those kills land inside the data backfill that older databases
need. That covers the history database schema only, not a full container
upgrade from those releases.

Every history schema change so far only adds columns, indexes and tables, so
the previous release can still open an upgraded database. If a future release
makes a change the previous release cannot read, its upgrade notes will say so,
and rolling back means restoring the backup taken before the upgrade. Anything
recorded after the upgrade is lost. There are no down-migrations. See
[[Roll back after an incompatible history change|History-Maintenance-and-Recovery#roll-back-after-an-incompatible-history-change]].

Windows hosts, including Docker Desktop, are best-effort. CI does not run the
app or its tests on Windows.

## Rolling back a release

Set `JBOD_UI_IMAGE` back to the recorded tag or digest and run the same `pull`
and `up -d`. Durable data is not rolled back with the image; see
[[Rolling back a release|Docker-and-GHCR-Deployment#rolling-back-a-release]]
for what that means for history.
