# Docker And GHCR Deployment

This page is the copy/paste deployment runbook for the published Docker image.

GHCR is GitHub's container registry. For this project it means:

- no repo clone for normal installs
- no local image build for normal updates
- the same image tag runs the main UI, history sidecar, and admin sidecar
- public pulls from `ghcr.io/gcs8/truenas-jbod-ui` do not require
  `docker login`

For the shortest first install, use [[Quick Start|Quick-Start]]. Use this page
when you want the fuller Docker runbook: tag pinning, sidecars, updates,
health checks, and the persistent folders to keep.

## Normal Install Shape

Pick a folder on the Docker host and keep the Compose file, `.env`, config, and
runtime data there. The examples below use `/docker-local/truenas-jbod-ui`.

```bash
sudo mkdir -p /docker-local/truenas-jbod-ui
sudo chown "$USER":"$USER" /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui
mkdir -p config/ssh data history/backups/long-term logs
```

Download the release Compose file and ownership helper from the same source
revision: the release tag you are going to run. Pin `JBOD_UI_IMAGE` in `.env`
to that same tag. The Compose file on `main` is for
images built from `main`; do not pair it with a release image.

```bash
mkdir -p scripts
tag=v0.22.2
curl -fsSL \
  -o compose.yaml \
  "https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/$tag/docker-compose.yml"
curl -fsSL \
  -o scripts/prepare_nonroot_bind_mounts.py \
  "https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/$tag/scripts/prepare_nonroot_bind_mounts.py"
```

Create a minimal `.env` for one TrueNAS system:

```bash
umask 077
cat > .env <<'EOF'
APP_PORT=8080
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2

TRUENAS_HOST=https://truenas.example.local
TRUENAS_API_KEY=replace_me
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false

SSH_ENABLED=false
EOF
chmod 600 .env
```

Prepare the bind mounts, then pull and start:

```bash
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001 --apply
docker compose pull
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

`v0.22.2` does not enforce `ADMIN_AUTH_MODE` on main-UI writes. Its mapping,
alias, import, locator, and LED routes remain writable, so do not treat network
mode as read-only. Restrict port `8080` to trusted clients.

Current `main` rejects these writes unless `ADMIN_AUTH_MODE=basic`, valid shared
credentials are supplied, and `APP_PUBLIC_ORIGIN` matches the exact main-UI
origin. This server-side policy is not in the published `v0.22.2` image. For a
source-built current-main image, configure:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
APP_PUBLIC_ORIGIN=https://storage-ui.example.local
```

Current `main` leaves the controls enabled and reports a rejected request
through the existing error path. The pre-click and post-401/403 disabling
behavior proposed in PR #322 is not in `v0.22.2` or current `main`.

On current `main`, Basic mode protects persistent and hardware-changing main-UI
writes while reads remain anonymous. Use HTTPS through a reverse proxy or an
encrypted private network; Basic credentials are not encrypted by HTTP itself.

## Default non-root runtime

The base Compose file uses numeric UID/GID `10001:10001` for the UI and history
services by default, and mounts the UI's configuration read-only. Every service
uses a read-only image filesystem with a private `/tmp`, drops all capabilities,
and enables `no-new-privileges`.

History, admin, and one-shot backup route `tempfile` workspaces to their existing
disk-backed history or backup state mounts. This preserves multi-gigabyte FULL
backup and restore headroom instead of charging those files to the `/tmp` tmpfs.

The admin sidecar remains explicitly root because it controls the raw Docker
socket and must preserve file ownership during transactional restores. It adds
back only `CHOWN` and `FOWNER`, uses the app-data GID as its primary group, and
can write only the mounted config, data, and history state plus the Docker
socket. Admin-generated private SSH keys use owner/group-readable mode `0640`
so the non-root UI can use them. The admin container no longer mounts the app
log directory. The one-shot backup service keeps its separate `1000:1000`
primary identity and receives only the supplemental app-data group `10001`.

For an existing deployment, stop the stack and run the ownership helper before
the first start on the non-root Compose file (unreleased, on `main`). Run it
once without `--apply` first. It inspects only
the `config` directory inode,
`config/config.yaml`, `config/ssh/**`, `config/tls/**`, `data/**`,
`history/**`, and `logs/**`. It deliberately leaves
`config/backup-secrets/**`, `backups/**`, and `backup-status/**` owned by the
backup identity. It refuses missing-path symlinks, special files, stale
replacement artifacts, more than 100,000 entries, or an apply larger than the
process descriptor budget:

```bash
docker compose down
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001 --apply
```

Apply opens and inode-checks every selected entry before its first ownership
change, keeps those descriptors open through verification, and restores the
original owners and modes if a later change fails. Selected directories become
`0770`; selected regular files become `0660`. This lets the app identity write
its runtime state and lets the backup service read selected app data through
its supplemental group. Backup passphrases remain under the separate backup
identity with their existing private modes.

Then start the base Compose file:

```bash
docker compose pull
docker compose up -d
docker compose exec enclosure-ui id
```

`docker-compose.nonroot.yml` remains available so existing immutable-deployment
chains do not need an immediate file-list change. It now repeats the base
non-root identity, read-only UI config mount, and backup group, so it is optional
and has no additional effect.

After any admin-sidecar restore, leave UI/history stopped, rerun the helper
preflight and apply step, then restart. This is the ownership interlock for
root-created restored files. If the helper reports that rollback was incomplete,
do not start any profile; preserve the full backup and inspect the named host
paths first.

To roll back, use the deployment transaction's retained predecessor Compose and
image identities. Do not recursively change ownership back unless that reviewed
predecessor requires it. Keep the pre-migration ownership listing and a full
encrypted backup until the non-root deployment has passed operator acceptance.

## Optional File-Backed Secrets

The base Compose file keeps `.env` compatibility. For service-scoped secret
files, download `docker-compose.secrets.yml` from the same release ref as the
base Compose file and create its ignored source directory:

```bash
umask 077
mkdir -p secrets
$EDITOR secrets/truenas_api_key
$EDITOR secrets/truenas_api_password
$EDITOR secrets/ssh_password
$EDITOR secrets/ssh_sudo_password
$EDITOR secrets/admin_auth_password
chmod 600 secrets/*
```

Create all five files before applying the overlay. An unused optional secret
may be an empty private file. Do not set a blank `_FILE` path: that is treated
as a startup error. The overlay mounts all five files into both UI and admin;
history and scheduled backup receive none.

Supported variables are `TRUENAS_API_KEY_FILE`,
`TRUENAS_API_PASSWORD_FILE`, `SSH_PASSWORD_FILE`,
`SSH_SUDO_PASSWORD_FILE`, and `ADMIN_AUTH_PASSWORD_FILE`. A file-backed value
takes precedence over its direct environment value. The loader rejects
symlinks, non-regular or group/world-writable files, invalid UTF-8, NUL,
and values larger than 64 KiB. It preserves whitespace except one final LF or
CRLF.

Start with both Compose files:

```bash
docker compose -f compose.yaml -f docker-compose.secrets.yml pull
docker compose -f compose.yaml -f docker-compose.secrets.yml up -d
```

This compatibility path applies only to the top-level single-system process
variables. It does not externalize saved multi-system or BMC credentials from
`config/config.yaml`. Keep that file and admin-generated backups protected.

To roll back, stop the stack, omit `docker-compose.secrets.yml`, restore the
matching direct values in `.env`, confirm `chmod 600 .env`, and recreate the
same profiles. The base Compose file is unchanged by the overlay.

## What Stays On Your Host

The image is disposable. Your local folder is the part you keep.

| Path | Why it matters |
| --- | --- |
| `compose.yaml` | service definitions and volume mounts |
| `.env` | image tag, ports, first system, and runtime knobs |
| `config/` | saved systems, profiles, TLS trust, runtime overrides |
| `config/ssh/` | SSH keys mounted read-only into the containers |
| `data/` | app cache and local support data |
| `history/` | history sidecar SQLite DB and backups |
| `logs/` | app log files when file logging is enabled |

Back up this folder, not the container image.

## Pick An Image Reference

For most home labs, start with:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest
```

That tracks the newest published stable image. Whatever tag you pick, download
`compose.yaml` from the matching release tag; the Compose file on `main` is for
images built from `main`.

If you want slower, more deliberate updates, select a release tag first:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2
```

Useful tag shapes:

| Tag | Use it when |
| --- | --- |
| `latest` | you want the newest stable published image |
| `v0.22.2` | you want the image currently labeled with that GitHub release |
| `0.22.2` | you want the same stable release without the `v` prefix |
| `dev` | you are testing the current development image and accept churn |

Every registry tag is a mutable pointer, including `latest`, version tags, and
`sha-...` tags. Release automation intends version tags to stay stable, but the
registry can still repoint them. Only a digest reference is immutable:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui@sha256:<64-hex-digest>
```

The GHCR publish workflow records this exact reference under **Immutable
manifest digest** in its job summary. Use that receipt, or resolve and verify the
tag locally as shown below, before a controlled deployment.

## Update The App By Digest

An image digest does not freeze a Compose file downloaded from `main`. A
controlled update must bind the image to the **Source revision** from the same
GHCR workflow receipt. Use the repository's transaction helper instead of
assembling separate shell snippets.

Download the helper itself from that exact source revision:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
printf '%s\n' "$release_revision" | grep -Eq '^[0-9a-f]{40}$'
test ! -L scripts
install -d -m 700 scripts
helper_tmp="$(mktemp scripts/.update-immutable-deployment.XXXXXX)"
trap 'rm -f "$helper_tmp"' EXIT
curl -fsSL \
  -o "$helper_tmp" \
  "https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/$release_revision/scripts/update_immutable_deployment.py"
chmod 700 "$helper_tmp"
python3 "$helper_tmp" --help >/dev/null
mv -f "$helper_tmp" scripts/update_immutable_deployment.py
trap - EXIT
```

The update command takes the complete ordered Compose chain, active profiles,
expected running services, and local health endpoints. This example is for the
base file plus the non-root overlay with history active:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'
expected_image='REPLACE_WITH_WORKFLOW_IMMUTABLE_IMAGE'
candidate_tag='ghcr.io/gcs8/truenas-jbod-ui:v0.22.2'
python3 scripts/update_immutable_deployment.py update . \
  --project-name truenas-jbod-ui \
  --source-revision "$release_revision" \
  --expected-image "$expected_image" \
  --candidate-tag "$candidate_tag" \
  --compose docker-compose.yml=compose.yaml \
  --compose docker-compose.nonroot.yml=docker-compose.nonroot.yml \
  --profile history \
  --service enclosure-ui \
  --service enclosure-history \
  --health-url http://127.0.0.1:8080/livez \
  --health-url http://127.0.0.1:8080/healthz \
  --health-url http://127.0.0.1:8081/healthz
```

Adjust the arguments to match the running deployment exactly:

- list every active tracked Compose file with one ordered `--compose
  SOURCE=LIVE` argument;
- pass the live `com.docker.compose.project` value with `--project-name`;
- list every active profile with `--profile`;
- list every expected running app service with `--service`;
- list each enabled loopback health endpoint with `--health-url`.

For example, add `docker-compose.secrets.yml=docker-compose.secrets.yml` when
the secrets overlay is active. Add `--profile admin`, `--service
enclosure-admin`, and its loopback health URL only when admin is expected to be
running through the update. The helper stops before mutation if the declared
service set differs from Compose's running service set.

The helper then performs one bounded transaction. It:

1. validates the Compose project name, working directory, ordered file chain,
   and exact running service set against every live container's Compose labels;
2. records each service's container image ID, immutable GHCR digest, health,
   and restart count;
3. requires all declared app services to share one rollback digest;
4. pulls the moving candidate tag and requires its sole GHCR `RepoDigest` to
   equal the full immutable image from the workflow receipt;
5. downloads every declared Compose source from the same exact 40-hex source
   revision and validates the candidate chain;
6. writes a private `0700` `.jbod-ui-image-update` directory containing a
   strict `0600` JSON receipt plus hashed previous and candidate Compose and
   `.env` bytes;
7. activates the candidate bytes and digest, then requires the exact service
   set, image IDs, health, zero restart counts, and health URLs to converge.

The receipt is JSON data, not shell code. The helper rejects symlinks, wrong
owners or modes, duplicate or extra keys, missing or extra files, hash changes,
and cardinality mismatches before verify or rollback touches Docker. An existing
receipt blocks another update so a rerun cannot erase rollback evidence.

### Verify Runtime Convergence

Re-read the private receipt and verify the running deployment again:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py verify .
```

Keep `.env` pinned to the full `name@sha256` value. Record the helper's source
revision, candidate digest, per-service image IDs, health results, and restart
counts in the release evidence.

### Rollback To The Previous Digest

Any failure after receipt publication triggers automatic rollback. The helper
restores every prior Compose file, pins `.env` to the recorded previous digest,
recreates the exact recorded profiles and services, and verifies image IDs,
health, restart counts, and health URLs before reporting rollback complete.

You can request the same receipt-validated rollback later:

```bash
set -euo pipefail
cd /docker-local/truenas-jbod-ui
python3 scripts/update_immutable_deployment.py rollback .
```

Keep the receipt through operator acceptance. Before a later update, archive
the whole `0700` receipt directory outside the deployment directory. Never
retag or overwrite a failed candidate digest.

## Main UI

The main UI runs by default:

```bash
docker compose pull
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

Health checks:

```bash
curl http://your-docker-host:8080/livez
curl http://your-docker-host:8080/healthz
```

`/livez` should answer quickly when the container is alive. `/healthz` is the
better operator view when the UI is up but a backend, host, cache, or sidecar
looks suspicious.

## Optional History Sidecar

Turn on history when you want slot-history charts, timeline heat maps, and
offline snapshots with historical samples:

```bash
docker compose --profile history pull
docker compose --profile history up -d
```

The main UI stays on `:8080`. The history sidecar listens on
`127.0.0.1:8081` by default and stores its database at:

```text
./history/history.db
```

Open the sidecar dashboard from the Docker host:

```text
http://127.0.0.1:8081
```

If Docker is on another machine, leave it bound to localhost unless you have a
reason to expose it. Use a tunnel, reverse proxy, or set
`HISTORY_BIND_ADDRESS=0.0.0.0` intentionally.

Use [[History and Snapshot Export|History-and-Snapshot-Export]] for the visual
walkthrough.

Automatic history permission repair is **disabled by default**. The sidecar
does not silently widen `history/`, the SQLite database, or its WAL/SHM files.
Prefer fixing the host directory's owner and group deliberately. Before a
migration, stop history and record the current contract:

```bash
docker compose --profile history stop enclosure-history
stat -c '%U:%G %a %n' history history/history.db 2>/dev/null || true
```

If a one-time in-container mode repair is required, set these values in `.env`:

```dotenv
HISTORY_PERMISSION_REPAIR_ENABLED=true
HISTORY_SHARED_DIR_MODE=0770
HISTORY_SHARED_FILE_MODE=0660
```

Modes use octal digits. Configured modes are not world-writable. Start history, verify
the recorded paths and `/healthz`, then set
`HISTORY_PERMISSION_REPAIR_ENABLED=false` and recreate the sidecar so later
read-only failures remain visible instead of triggering another chmod:

```bash
docker compose --profile history up -d enclosure-history
stat -c '%U:%G %a %n' history history/history.db
curl -fsS http://127.0.0.1:8081/healthz
```

To **roll back**, stop the sidecar, disable repair, restore the owner/group and
modes recorded before migration, restore the previous image tag if needed, and
recreate `enclosure-history`. Do not use `0777` or `0666` as a workaround.

## Optional Admin Sidecar

Turn on admin when you want guided setup, storage-view editing, backup/restore,
runtime controls, or the profile builder:

Before starting it, read the
[Admin trust boundary](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/ADMIN_TRUST_BOUNDARY.md). The default network
mode has no application login and treats every client that can reach port
`8082` as a trusted operator. The mounted Docker socket gives the sidecar
host-level container authority. Restrict network reachability to trusted
operators. Auto-stop limits exposure; it is not authentication.

### Admin browser origin

Set `ADMIN_PUBLIC_ORIGIN` in `.env` before starting the admin profile. Use the
exact origin the browser shows for the admin UI: scheme, host, and port, with no
path, for example `http://jbod-admin.example.test:8082` for the default port
publication or `https://jbod-admin.example.test` behind a reverse proxy.

Browser-initiated admin changes (POST, PUT, PATCH, DELETE) are accepted only
when their `Origin` or `Referer` header matches this value; any other browser
request is rejected with `403 Cross-origin admin mutation rejected.` The
published Compose file passes the variable through empty. `v0.22.2` and current
`main` still start when the value is empty or malformed, but browser mutations
are rejected at request time with `403 Cross-origin admin mutation rejected.`
Set it before using the admin UI. It is required in both `network` and `basic`
mode. Startup refusal for an empty or invalid value is proposed in PR #321; it
is not part of `v0.22.2` or current `main`.

```dotenv
ADMIN_PUBLIC_ORIGIN=http://jbod-admin.example.test:8082
```

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

The application default for `ADMIN_AUTO_STOP_SECONDS` is `0`, which never
stops the sidecar. The published Compose files set `3600`, so a Compose-started
sidecar stops itself after one hour unless you change the value in `.env`. Set
it explicitly if you run the sidecar outside those files.

```dotenv
ADMIN_AUTO_STOP_SECONDS=3600
```

`ADMIN_AUTO_STOP_SECONDS=0` disables auto-stop. Positive integers set the
timeout in seconds; negative or malformed values fail startup validation. After
changing the environment value, recreate the admin container so its process
environment is updated:

```bash
docker compose --profile admin up -d --force-recreate enclosure-admin
```

Use [[Admin UI and System Setup|Admin-UI-and-System-Setup]] for the walkthrough.

## Start Everything

If you want the main UI plus both optional sidecars:

```bash
docker compose --profile history --profile admin pull
docker compose --profile history --profile admin up -d
```

## Ports

| Service | Default host port | Notes |
| --- | --- | --- |
| main UI | `8080` | set `APP_PORT` to change it |
| history sidecar | `8081` | binds to `127.0.0.1` unless `HISTORY_BIND_ADDRESS` changes |
| admin sidecar | `8082` | set `ADMIN_PORT` to change it |

Keep the history sidecar localhost-only unless you actually need to scrape or
open it from another machine.

## Logs And Operations Hooks

For a quick local read:

```bash
docker compose logs --tail=150 -f
```

For one service:

```bash
docker compose logs --tail=150 -f enclosure-ui
docker compose logs --tail=150 -f enclosure-history
docker compose logs --tail=150 -f enclosure-admin
```

Day-two logging, syslog, Prometheus/OpenMetrics, and Grafana notes live in:

- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]

Common knobs:

```dotenv
LOG_FORMAT=text
METRICS_ENABLED=true
METRICS_PATH=/metrics
HISTORY_BIND_ADDRESS=127.0.0.1
```

## Common Fixes

If Compose complains about missing `.env` values, create or repair `.env` in
the same folder as `compose.yaml`.

If the app starts but cannot talk to TrueNAS, check:

- the `TRUENAS_HOST` URL
- the API key
- Docker-host network access to TrueNAS
- whether the TrueNAS certificate needs `TRUENAS_VERIFY_SSL=false` or a trusted
  CA bundle

If the browser still shows an old UI after an update:

```bash
docker compose pull
docker compose up -d
docker compose restart enclosure-ui
```

Then hard-refresh the browser tab.

For more symptom-driven fixes, use [[Troubleshooting]].

## Advanced Source Builds

Most users should stay on the published image path above.

Clone the repo and use the dev Compose file only when you are editing the app,
testing branch changes, or intentionally rebuilding the image locally:

```bash
git clone https://github.com/gcs8/truenas-jbod-ui.git
cd truenas-jbod-ui
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

Edit `.env` before the first start; values in `.env` override matching YAML settings.
Replace the example connection values there, or remove an environment value when
you intend `config/config.yaml` to own that setting.

```bash
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml --profile history up -d --build
docker compose -f docker-compose.dev.yml --profile admin up -d --build enclosure-admin
```

## Related Pages

- [[Quick Start|Quick-Start]]
- [[Visual Tour|Visual-Tour]]
- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[Troubleshooting]]
