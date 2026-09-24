# Troubleshooting

This page is the short list of the failures you are most likely to hit.

Run these commands from the folder where your `compose.yaml` and `.env` live.
If Docker is on another machine, replace `localhost` with that host name or IP.

## Start here

Check that the containers are actually running:

```bash
docker compose ps
```

Check the main UI health paths:

```bash
curl http://localhost:8080/livez
curl http://localhost:8080/healthz
```

Follow recent logs:

```bash
docker compose logs --tail=150 -f
```

If `livez` is not `ok`, fix the container/runtime problem first. If `livez` is
healthy but `healthz` reports a warning or degraded dependency, read that
payload before chasing layout bugs.

## A container keeps restarting

`docker compose up -d` reports success even when a container fails as soon as
it starts. Check:

```bash
docker compose ps
docker compose logs --tail=50 enclosure-ui
```

A container that shows `Restarting` in `docker compose ps` has a startup
error. Read the last line of its log: it names the setting or folder that
stopped it. Use `enclosure-history` or `enclosure-admin` in place of
`enclosure-ui` for the other services. The admin container does not restart
on its own; it shows `Exited` instead.

## The app starts but the UI looks empty

Common causes:

- the `.env` host, API key, or credentials are wrong
- the Docker host cannot reach the TrueNAS host
- the selected system has no discovered enclosure yet
- the app is waiting on a slow first inventory pass

Good next steps:

- check the status chips near the top of the UI
- confirm the selected system in the header
- run `docker compose logs --tail=150 enclosure-ui`
- open `http://your-docker-host:8080/healthz`

## The app is up but slot mapping looks wrong

Common causes:

- SSH enrichment is off
- SES access is missing
- the wrong profile is selected
- a system needs explicit `enclosure_profiles`
- a generic Linux host needs `slot_hints`

Good next steps:

- check the warning banner
- confirm the selected system and enclosure
- inspect `config/config.yaml`
- confirm the SSH user can run the exact inventory commands shown in the admin
  setup page

### Legacy unscoped mappings after an upgrade

Legacy `default:{slot}` mappings are used only when one system is configured and
exactly one physical enclosure is discovered. That is the only case where the
old row can be attributed without choosing between systems or enclosures.

If disks are visible but no physical enclosure can be identified, the UI groups
only the known disk evidence in a system-scoped virtual inventory. Its order is
not a physical bay or slot map, and legacy mappings are not applied. The stored
rows are retained. After enclosure discovery is restored, select the affected
physical enclosure and re-save each mapping so it is stored with system and
enclosure scope. Multi-system and multi-enclosure deployments likewise deny the
legacy fallback and show one bounded warning with the affected count and the same
re-save guidance.

## The UI says a sudo command is not allowed

That means the app tried to run a command the SSH user cannot execute.

Fix it by:

- adding the exact command to sudoers
- or deciding you do not want that feature on that host

Do not broaden sudo more than needed.

## Main-UI writes return 401 or 403

In Basic mode, `Read UI authentication required.` means the page is signed out
or the credentials are wrong. Sign in again on that page. A cross-origin error
means `APP_PUBLIC_ORIGIN` does not exactly match the scheme, host, and port in
the browser address bar.

## Admin mutations return 403

`This page was opened at ..., but the admin service only accepts changes from
...` means the browser origin does not match the admin address. The message
names both addresses. Without `ADMIN_PUBLIC_ORIGIN`, the service compares it with
the request's own scheme, host, and port. Reverse-proxy deployments can set
`ADMIN_PUBLIC_ORIGIN` to the exact public address shown in the browser, with no
path. Basic mode requires that setting and refuses to start if it is empty or
malformed.

## Full backup returns 400

`Plaintext backup export is disabled.` means an unsanitized export was requested
without encryption. Enable encryption. Set
`ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true` only for an intentional,
access-restricted workflow; the override does not make the archive safe to share.

## Saves fail and the history service keeps restarting

Docker creates a missing bind mount as `root:root`. The containers run as uid
10001, so nothing can be written until the host directories are owned by that
uid. What you see:

- Every mapping or alias save fails with
  `Could not save: the data folder is not writable by the app. See
  Troubleshooting.` (HTTP 503). Before v0.24 this was a generic HTTP 500.
- The history service logs
  `Cannot write to /app/history (owned by uid 0, running as uid 10001). On the
  Docker host run: sudo chown -R 10001:10001 ./history`, retries a few times
  with growing backoff, and then stops with that same line instead of looping a
  traceback.

The log line already names the host path: the container sees `/app/history`,
but compose binds `./history` from the directory holding `docker-compose.yml`,
so that is what you chown. Run it from that directory, for every bound
directory at once:

```bash
sudo chown -R 10001:10001 ./config ./data ./history ./logs ./backup-status
docker compose up -d
```

Create the directories before the first `docker compose up -d` to avoid this
entirely:

```bash
mkdir -p config/ssh data history logs backup-status
```

## A non-root container gets permission denied

The default `docker-compose.yml` runs the UI and history as root, so a normal
image update needs no ownership change. Non-root services come only from the
optional `docker-compose.nonroot.yml` overlay. If you added that overlay and a
service now reports `permission denied`, the bind mounts are still owned by
root. From the folder that holds your Compose files, with a published image and
no repository checkout:

```bash
docker compose down
app_uid="${APP_UID:-10001}"
app_gid="${APP_GID:-10001}"
backup_uid="${BACKUP_UID:-1000}"
sudo find ./config -path ./config/backup-secrets -prune -o -exec chown "$app_uid:$app_gid" {} +
sudo chown -R "$app_uid:$app_gid" ./data ./logs ./history
sudo install -d -o "$backup_uid" -g "$app_gid" -m 2750 ./backup-status
docker compose -f docker-compose.yml -f docker-compose.nonroot.yml up -d
```

If `.env` sets `APP_UID`, `APP_GID` or `BACKUP_UID`, set the same values above;
shell variables do not read `.env`. If `HISTORY_SEGMENT_CATALOG_PATH` is set, do
not run a recursive change over the segmented history tree; follow
[[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]] instead.
To undo, drop the overlay from the `-f` chain; root-run services still read
files owned by the app identity.

From a source checkout you can use the bounded helper instead, which also sets
modes and refuses symlinks and unexpected paths:

```bash
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid "$app_uid" --gid "$app_gid"
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid "$app_uid" --gid "$app_gid" --apply
```

Run the dry check first. Do not use recursive `chmod 777`. If SSH then fails to
load `known_hosts`, verify that `data/known_hosts` (or the file named by
`ssh.known_hosts_path` / `SSH_KNOWN_HOSTS_PATH`, if set) is owned by the
configured app UID/GID and uses mode `0660`.

## SCALE shows a generic runtime profile

That usually means:

- no built-in profile was matched
- or the system config is missing explicit `enclosure_profiles`

Fix it by binding the known enclosure IDs to the front/rear built-in profiles.

Example:

```yaml
enclosure_profiles:
  "5003048001c1043f": supermicro-ssg-6048r-front-24
  "500304801e977aff": supermicro-ssg-6048r-rear-12
```

## A Linux host has no SES devices

That is not fatal.

It means:

- no SES slot mapping from `/dev/sg*`
- likely no SES-driven LED control

The host can still be useful as:

- an SSH inventory target
- a profile-driven physical layout
- an `mdadm` or NVMe topology target

## SMART fields are missing

Check whether:

- the platform exposes them at all
- SSH enrichment is enabled
- `smartctl` sudo is allowed
- `nvme-cli` sudo is allowed for Linux NVMe enhancement

## The history button is missing

The history sidecar is optional. If you expected it to be running:

```bash
docker compose --profile history ps
docker compose --profile history logs --tail=150 enclosure-history
curl http://localhost:8081/livez
```

Start or update it with:

```bash
docker compose --profile history pull
docker compose --profile history up -d
```

## History says permission denied or readonly database

`Permission denied: '/app/history/history.db'` or `attempt to write a readonly
database` in the history log means the `history` folder or the database file is
not writable by the user the container runs as. With the default Compose file
that is root, so check for a read-only mount or file system first. With the
non-root overlay it is `APP_UID:APP_GID`; fix ownership as described in
[A non-root container gets permission denied](#a-non-root-container-gets-permission-denied).
Do not use `chmod 777`.

## History refuses to start after changing HISTORY_BIND_ADDRESS

`Non-loopback history exposure requires refresh token mode.` in the history log
means `HISTORY_BIND_ADDRESS` is no longer loopback but the token settings are
missing. Set token mode, a token and `HISTORY_PUBLIC_ORIGIN` together as shown in
[[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]], then recreate the
container:

```bash
docker compose --profile history up -d --force-recreate enclosure-history
```

To go back to localhost only, remove `HISTORY_BIND_ADDRESS` from `.env` and
recreate the container the same way.

## The admin page is missing

The admin sidecar is optional. If you expected it to be running:

```bash
docker compose --profile admin ps
docker compose --profile admin logs --tail=150 enclosure-admin
curl http://localhost:8082/livez
```

Start or update it with:

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Then open:

```text
http://your-docker-host:8082
```

## The browser keeps showing old UI

Try these in order:

1. hard refresh the browser tab
2. open a private/incognito tab
3. pull the current published image and recreate the container

```bash
docker compose pull
docker compose up -d
```

If the container is already current but the browser still looks stale:

```bash
docker compose restart enclosure-ui
```

Only use `--build` or `docker-compose.dev.yml` if you intentionally cloned the
repo and are running a source-build setup.

## Multipath or pool grouping looks wrong on CORE

Check whether the SSH user can run:

```text
gmultipath list
camcontrol devlist -v
zpool status -gP
```

Those are the commands that usually fill in the missing context.
