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

## Saving a bay assignment fails with 500

`Unhandled application error` after saving a bay assignment means the main UI
cannot write to its `data` folder. The usual cause is a folder owned by root
while the container runs as the app user. Fix the ownership as described in
[A non-root container gets permission denied](#a-non-root-container-gets-permission-denied),
then save again.

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

`Cross-origin admin mutation rejected.` means the browser origin does not match
the admin address. Without `ADMIN_PUBLIC_ORIGIN`, the service compares it with
the request's own scheme, host, and port. Reverse-proxy deployments can set
`ADMIN_PUBLIC_ORIGIN` to the exact public address shown in the browser, with no
path. Basic mode requires that setting and refuses to start if it is empty or
malformed.

## Full backup returns 400

`Plaintext backup export is disabled.` means an unsanitized export was requested
without encryption. Enable encryption. Set
`ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true` only for an intentional,
access-restricted workflow; the override does not make the archive safe to share.

## A non-root container gets permission denied

The v0.23.0 Compose file runs the main UI and history as the app user.
Permission errors can be caused by ownership, modes or mount configuration;
confirm the effective service identity and failing path first. A normal image
update that retains a root-compatible Compose file does not require an optional
non-root ownership migration.

If `HISTORY_SEGMENT_CATALOG_PATH` is set, stop here and use the segmented repair
procedure in [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]].
Do not apply this helper to sealed segments.

1. Save a private pre-upgrade backup of the state, the previous image digest
   or tag from `JBOD_UI_IMAGE`, the environment file, and every selected Compose
   file. Record the project, ordered `-f` file chain, `--env-file`, active
   `--profile` selections and service names. Retain those selections on every
   Compose command; do not enable previously inactive admin or backup services.
2. Stop all writers, including scheduled backup jobs and admin, before changing
   ownership. Use the recorded `docker compose --project-name ... --env-file ...
   -f ... --profile ... stop` selection, not a different default project.
3. Obtain `scripts/prepare_nonroot_bind_mounts.py` from the v0.23.0 source at
   commit `dbcd2ab31e9a46083693cf08802434ac8cfc1e43`. Replace the checkout path
   below with that verified local checkout path. If the exact helper cannot be
   obtained, stop; do not replace it with recursive ownership commands.
   Set `app_uid` and `app_gid` explicitly to the effective `APP_UID` and
   `APP_GID` of the selected non-root services. Shell variables do not
   automatically read `.env`; do not assume defaults override configured IDs.
4. Run a dry run against the deployment directory, then use `--apply` only if
   the preflight succeeds and the path scope and identities are correct:

   ```bash
   sudo python3 /path/to/v0.23.0-checkout/scripts/prepare_nonroot_bind_mounts.py . --uid "${app_uid:?set the effective APP_UID}" --gid "${app_gid:?set the effective APP_GID}"
   sudo python3 /path/to/v0.23.0-checkout/scripts/prepare_nonroot_bind_mounts.py . --uid "${app_uid:?set the effective APP_UID}" --gid "${app_gid:?set the effective APP_GID}" --apply
   ```

   The helper covers `data`, `history`, and `logs` recursively, the `config`
   directory itself, and only `config/config.yaml`, `config/ssh`, and
   `config/tls` beneath it. It excludes `config/backup-secrets` and other
   unlisted config children. It sets directories to `0770` and files to
   `0660` as well as their owner/group. A chown-only substitute is not equivalent.
   Stop on a rejected symlink, stale artifact, unsupported platform or exceeded
   bound; do not bypass the preflight. Use this only for the matching local
   POSIX bind-mount layout, not custom paths or other storage backends.
5. Restart only the previously active services with the same recorded Compose
   project, environment, ordered files and profiles, using `up -d` and the
   recorded service names. Check their health and state before resuming jobs.

Rollback restores the previous `JBOD_UI_IMAGE` pin and every changed Compose
file, using the same `--project-name`, `--env-file`, ordered `-f` and `--profile`
selection for `pull` and `up -d` with the previous service names. This restores
runtime selection, not data-format compatibility. If an older image cannot use
the updated state, keep writers stopped and follow its recovery procedure with
the private pre-upgrade backup. Do not delete history to make startup succeed.

Do not use recursive `chmod 777`. If SSH fails to load `known_hosts`, verify
that `data/known_hosts` is owned by the configured app UID/GID with mode `0660`.

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
database` in the history log means the `history` folder or the database file
is not writable by the user the container runs as. With the v0.23.0 Compose
file that user is `APP_UID:APP_GID`. Give the folder to that user as described
in
[A non-root container gets permission denied](#a-non-root-container-gets-permission-denied),
or use the root-compatible v0.22.2 Compose file. Do not use `chmod 777`.

## History refuses to start after changing HISTORY_BIND_ADDRESS

`Non-loopback history exposure requires refresh token mode.` in the history
log means `HISTORY_BIND_ADDRESS` is no longer `127.0.0.1` but the token
settings are missing. History listens off-loopback only with all of these in
`.env`:

```dotenv
HISTORY_REFRESH_AUTH_MODE=token
HISTORY_REFRESH_TOKEN=replace-with-a-long-private-value
HISTORY_PUBLIC_ORIGIN=http://your-docker-host:8081
```

Use `HISTORY_REFRESH_TOKEN_FILE` with the secrets overlay instead of
`HISTORY_REFRESH_TOKEN` when you keep the token in a file. Then recreate the
history container:

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
