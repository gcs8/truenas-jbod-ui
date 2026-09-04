# Troubleshooting

This page is the short list of the failures you are most likely to hit.

Run these commands from the folder where your `compose.yaml` and `.env` live.
If Docker is on another machine, replace `localhost` with that host name or IP.

## Start Here

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

## The App Starts But The UI Looks Empty

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

## The App Is Up But Slot Mapping Looks Wrong

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

## The UI Says A Sudo Command Is Not Allowed

That means the app tried to run a command the SSH user cannot execute.

Fix it by:

- adding the exact command to sudoers
- or deciding you do not want that feature on that host

Do not broaden sudo more than needed.

## SCALE Shows A Generic Runtime Profile

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

## A Linux Host Has No SES Devices

That is not fatal.

It means:

- no SES slot mapping from `/dev/sg*`
- likely no SES-driven LED control

The host can still be useful as:

- an SSH inventory target
- a profile-driven physical layout
- an `mdadm` or NVMe topology target

## SMART Fields Are Missing

Check whether:

- the platform exposes them at all
- SSH enrichment is enabled
- `smartctl` sudo is allowed
- `nvme-cli` sudo is allowed for Linux NVMe enhancement

## The History Button Is Missing

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

## The Admin Page Is Missing

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

## Main UI write authorization differs by version

`v0.22.2` does not enforce `ADMIN_AUTH_MODE` on main-UI writes. If those writes
succeed in network mode, changing the variables below will not add main-UI write
protection to that release. Restrict port `8080` to trusted clients and upgrade
to a release that contains the current-main server policy when one is published.

Current `main` rejects these writes unless `ADMIN_AUTH_MODE=basic`. A rejected
network-mode request returns `Read UI mutations require
ADMIN_AUTH_MODE=basic.` Current `main` leaves the controls enabled and reports
the rejection through the existing error path; it does not disable them before
the click or after a 401/403. For a source-built current-main image, set:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
APP_PUBLIC_ORIGIN=http://your-docker-host:8080
```

`APP_PUBLIC_ORIGIN` must be the exact origin your browser shows for the main
UI. If it does not match, current-main writes fail with
`Cross-origin Read UI mutation rejected.` Recreate `enclosure-ui` after changing
these values. The disabled-control behavior is proposed in PR #322; it is not in
`v0.22.2` or current `main`.

## The Admin Page Rejects Every Change With 403

`Cross-origin admin mutation rejected.` means `ADMIN_PUBLIC_ORIGIN` is unset,
malformed, or does not match the origin your browser shows for the admin UI.
`v0.22.2` and current `main` still start in that state; browser mutations are
rejected at request time with `403 Cross-origin admin mutation rejected.` Set
the exact origin, for example `http://your-docker-host:8082`, then recreate
`enclosure-admin`:

```bash
docker compose --profile admin up -d --force-recreate enclosure-admin
```

Startup refusal for an empty or invalid value is proposed in PR #321; it is not
part of `v0.22.2` or current `main`.

## Full Backup Export Fails With 400

`Plaintext backup export is disabled.` means encryption was turned off for a
Full Backup. Exports must be encrypted unless the admin service runs with
`ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT=true`, which is meant only for a
trusted-operator deployment where the archive itself is protected separately.
Turn encryption back on, or set that variable and recreate `enclosure-admin`.

## Permission Errors After Switching To The Non-Root Compose File

The non-root Compose file (unreleased, on `main`) runs the UI and history
services as `10001:10001`. On a folder created by an older root-owned
deployment they cannot write `data/`, `history/`, or `logs/`, and the logs show
`PermissionError` or read-only database failures.

Stop the stack and run the ownership helper, dry run first:

```bash
docker compose down
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001 --apply
docker compose up -d
docker compose exec enclosure-ui id
```

See the non-root runtime section in
[[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]].

## The Browser Keeps Showing Old UI

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

## Multipath Or Pool Grouping Looks Wrong On CORE

Check whether the SSH user can run:

```text
gmultipath list
camcontrol devlist -v
zpool status -gP
```

Those are the commands that usually fill in the missing context.
