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
| Local state | Bind mounts on a local POSIX filesystem, owned by root; or, with the hardening overlay, handed to uid 10001 by the one-time ownership step in [[Troubleshooting]]. The history directory mounted read-only (`--scenario readonly-state`), or full with no free inodes on a small disposable `tmpfs` (`--scenario enospc-state`), when the new release starts: history refuses with HTTP 503 while both processes stay up, with no restarts and no change to history bytes, config or mappings | Network filesystems; a read-only or full config or UI data directory; a full disk that runs out of data blocks rather than inodes |
| Services | Main UI and history on the published base Compose file, and on the base file plus `docker-compose.nonroot.yml` | Admin and backup services during an upgrade |
| Upgrade path | v0.22.2 to the current build: change `JBOD_UI_IMAGE`, then `pull` and `up -d`, with the same `-f` files each time | Releases before v0.22.2, skipping several releases |
| After upgrading | Both containers healthy with no restarts; the new version and revision are reported; bay mappings, history rows and `config.yaml` unchanged; database integrity and schema compatibility checks pass; the report says `schema_transition=none` because the table layout and logical schema-version marker remain unchanged, while startup adds chronological indexes to the hot database; a v0.22.2 segmented catalog keeps the exact generation, catalog bytes and referenced segment bytes; no change of file ownership, and hardened services still run as uid 10001 | Large (multi-GiB) history databases, many enclosures, production-scale index construction time and headroom |
| Rollback | Pin back to v0.22.2 with the same two commands, on base, hardened and segmented-history deployments; the older release starts and reads what the newer one wrote | Rollback across a history schema change |
| Recovery | The new history container killed (`SIGKILL`) inside each schema-initialization step on v0.22.2 data, one after another; the next `up -d` comes up healthy with no restarts, integrity check `ok`, no rows lost, nothing quarantined, and the same database an uninterrupted start produces. After the read-only or full history fault is removed by hand, the same release opens the retained history with integrity check `ok` and no rows lost | A killed restore, a killed data backfill at runtime (v0.22.2 data needs none; see below), an encrypted restore on a clean host |

The checks are `scripts/run_image_upgrade_smoke.py`: the base upgrade runs in
the `Image-only upgrade smoke` CI job, and `--scenario hardened`,
`--scenario interrupted-migration` and `--scenario segmented-catalog` run as
separate steps of the `Hardened, interrupted and segmented upgrade smoke` job.
The segmented run uses v0.22.2's own migration module and reader, captures the
catalog generation plus exact catalog and segment digests, and requires the
same identity and combined history view after upgrade and rollback. v0.22.2
already has the current table layout and logical schema-version marker, so
the image-upgrade run checks that the candidate accepts and preserves them.
Startup adds five chronological indexes to the hot database: three from the
schema SQL and two disk-identity chronological indexes from startup's identity
index step. The two nonchronological identity indexes already exist in a
database started by v0.22.2; a released-SQL-only fixture also gains those two.
The real `HistoryStore` constructor test checks this complete delta, released
definitions and rows, unchanged logical schema version, and idempotent restart.
On a hot database with more than 250,000 history rows, startup defers the
missing indexes so the service answers its healthcheck first; the collector
then builds them one at a time before it collects, and history reads use the
older, slower path until they finish (#910). On shutdown the collector asks
SQLite to interrupt an in-flight deferred index build, waits for its connection
to close, and does not start another index or collection pass. The interrupted
index rolls back atomically; completed indexes remain and the next start builds
only the missing ones (#988). Cancellation is checked before execution and
every 1,000 SQLite VM instructions. SQLite writer-lock waits keep their existing
five-second timeout. This does not impose a wall-clock bound on stalled storage
I/O or rollback, and production-scale Docker shutdown timing remains unqualified.
Compose stop-grace settings are unchanged.
A 2-million-row synthetic
database took 14.6 seconds to start without the deferral; production's
4-million-row database took 58.7 seconds, past the healthcheck.
Executing the schema SQL alone does not qualify startup. Existing immutable
segments retain their exact bytes without reindexing and their older query path;
newly sealed segments include the indexes. Index construction time and disk
headroom on production-scale databases remain unqualified.
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

### Synthetic clean-host recovery driver

`scripts/run_image_upgrade_smoke.py --scenario clean-host-restore` adds a
manual qualification cell. It is not included in the CI jobs above, and its
Docker/image run remains unverified. Source tests do not change the recovery
row of the table or establish support, scale, promotion or human acceptance.

On an explicitly admitted exclusive disposable Linux Docker host, the driver
uses the existing v0.22.2 Compose and synthetic seed contracts. The predecessor
exports encrypted `7z` through Admin. The driver tears down its resources,
checks container/network/volume absence, and removes its entire installation
before creating a separate target from current Compose. The target starts
with different bootstrap config and no predecessor mappings or history.
Its current Admin inspect/import API requires the encrypted-mode inspection
receipt. Config and mapping bytes, history counts and logical disk identity,
integrity, history API reads, health, image/revision and restart survival are
checked. SQLite file-byte equality is not promised across snapshot/restore.

Inputs are the encrypted archive, its separate synthetic operator key input,
the public bootstrap fixture and current Compose/image/version information.
The original installation is not a recovery input. This cell uses legacy
`7z`, which the predecessor can export, not a claim that v0.22.2 exports the
new encrypted FULL stream format. Offline tests separately exercise the real
current `tar.zst.enc` exporter/importer without an original source tree.
The key input is separate from the archive and is not printed. This fixture
does not choose a password manager, recovery-key custody/loss policy or human
sign-off process. An independent docs-only walkthrough remains unverified.

After separate runtime admission, use the existing image-smoke arguments with
`--scenario clean-host-restore --disposable-clean-host-restore` and a new
`--root "$TMPDIR/jbod-clean-host-<unique-run>"`. The root must be absent,
below the selected scratch directory and free of symlink ancestors. Use
`tests/fixtures/compose/v0.22.2.yml` and
`tests/fixtures/ci-smoke-config.yaml` unchanged, the pinned predecessor image
from CI, and the admitted exact candidate image and OCI revision. The driver
uses only the local Docker socket and an empty task-owned Docker client config.
Reserve ports 18180, 18181 and 18182 and the existing JBOD container names.
Do not run on a shared or production daemon. No workflow or registry
publication is added by this driver.

Success removes task runtime resources and files. Any scenario failure,
teardown failure, residual resource or unknown census retains recovery files
and runtime configuration for diagnosis. Never replace an existing root or
erase retained recovery material merely to retry.

## Rolling back a release

Set `JBOD_UI_IMAGE` back to the recorded tag or digest and run the same `pull`
and `up -d`. Durable data is not rolled back with the image; see
[[Rolling back a release|Docker-and-GHCR-Deployment#rolling-back-a-release]]
for what that means for history.
