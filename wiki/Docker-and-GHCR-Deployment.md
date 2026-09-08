# Docker and GHCR deployment

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

## Normal install shape

Pick a folder on the Docker host and keep the Compose file, `.env`, config, and
runtime data there. The examples below use `/docker-local/truenas-jbod-ui`.

```bash
sudo mkdir -p /docker-local/truenas-jbod-ui
sudo chown "$USER":"$USER" /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui
mkdir -p config/ssh data history/backups/long-term logs
```

Download the v0.23.0 Compose file:

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/v0.23.0/docker-compose.yml
```

Create a minimal `.env` for one TrueNAS system:

```bash
umask 077
cat > .env <<'EOF'
APP_PORT=8080
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.23.0

TRUENAS_HOST=https://truenas.example.local
TRUENAS_API_KEY=replace_me
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false

SSH_ENABLED=false
EOF
chmod 600 .env
```

Pull and start:

```bash
docker compose pull
docker compose up -d
```

This path pairs the v0.23.0 Compose file and image. Keep the Compose file and
image on the same version.

Open:

```text
http://your-docker-host:8080
```

The default setup has no login. Anyone who can reach the published main or
admin port can use the controls available there. Do not publish those ports
directly to the Internet. Confirm the UI and health endpoints work before adding
optional hardening.

## Optional hardening after startup

To opt into Basic authentication, set shared credentials and the exact browser
origins:

```dotenv
ADMIN_AUTH_MODE=basic
ADMIN_AUTH_USERNAME=operator
ADMIN_AUTH_PASSWORD=replace-with-a-long-random-secret
APP_PUBLIC_ORIGIN=https://storage-ui.example.local
ADMIN_PUBLIC_ORIGIN=https://storage-admin.example.local
```

In Basic mode, both origin settings are required. Basic mode protects all admin
pages and persistent or hardware-changing main-UI writes while reads remain
anonymous. Use HTTPS through a reverse proxy or an encrypted private network;
Basic credentials are not encrypted by HTTP itself.

Each live page starts signed out. Use its in-page sign-in before a write. The
browser holds the credentials only in page memory, sends them only to
same-origin verification and mutation routes, and clears them on reload or
sign-out. Separate tabs and the dedicated Storage Fabric page require their own
sign-in.

Firewall rules and network segmentation can restrict who reaches the app, but
they do not authenticate a client. Use an HTTPS reverse proxy or encrypted
network path when traffic leaves the Docker host. To verify the appliance
certificate with a private CA, follow
[[Advanced Configuration|Advanced-Configuration]]. Add SSH only after API access
works and only when you want the enrichment or hardware controls described in
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]].

## Optional file-backed secrets

The base Compose file keeps `.env` compatibility. For service-scoped secret
files, download `docker-compose.secrets.yml` from the same version tag as the
base Compose file and create its ignored source directory:

```bash
umask 077
mkdir -p secrets
$EDITOR secrets/truenas_api_key
$EDITOR secrets/truenas_api_password
$EDITOR secrets/ssh_password
$EDITOR secrets/ssh_sudo_password
$EDITOR secrets/admin_auth_password
$EDITOR secrets/history_refresh_token
chmod 600 secrets/*
```

Create all six files before applying the overlay. An unused optional secret
may be an empty private file. Do not set a blank `_FILE` path: that is treated
as a startup error. The overlay mounts appliance, SSH, admin-auth, and history
refresh secrets only into the services that consume them: the history refresh token
reaches UI and history, but not admin or scheduled backup.

Supported variables are `TRUENAS_API_KEY_FILE`,
`TRUENAS_API_PASSWORD_FILE`, `SSH_PASSWORD_FILE`,
`SSH_SUDO_PASSWORD_FILE`, `ADMIN_AUTH_PASSWORD_FILE`, and
`HISTORY_REFRESH_TOKEN_FILE`. A file-backed value
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

## What stays on your host

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

## Pick an image reference

For most home labs, start with:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest
```

That tracks the newest published stable image.

If you want slower, more deliberate updates, select a release tag first:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.23.0
```

Useful tag shapes:

| Tag | Use it when |
| --- | --- |
| `latest` | you want the newest stable published image |
| `v0.23.0` | you want the image currently labeled with that GitHub release |
| `0.23.0` | you want the same stable release without the `v` prefix |
| `dev` | you are testing the current development image and accept churn |

Every registry tag is a mutable pointer, including `latest`, version tags, and
`sha-...` tags. Only a digest reference is immutable:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui@sha256:<64-hex-digest>
```

Resolve the digest for the tag you intend to deploy, then place the full
`name@sha256` reference in `.env` when you need an immutable image selection.

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

## Optional history sidecar

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
migration, stop history and record the current ownership and modes:

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

## Optional admin sidecar

Turn on admin when you want guided setup, storage-view editing, backup/restore,
runtime controls, or the profile builder:

Before starting it, read the
[Admin trust boundary](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/ADMIN_TRUST_BOUNDARY.md).
The default setup has no login. Anyone who can reach port `8082` can change
configuration and control the app's containers. The mounted Docker socket gives the sidecar
host-level container authority. Auto-stop limits exposure; it is not
authentication.

In Basic mode, set `APP_PUBLIC_ORIGIN` and `ADMIN_PUBLIC_ORIGIN` to the exact
addresses shown in the browser. The example earlier on this page includes all
required values.

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

The application default is `0`, which disables auto-stop. The shipped Compose
files explicitly set a Compose default of `3600` seconds, so normal Compose
launches stop the sidecar after one hour. Change that in `.env` only if you
intend different behavior:

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

## Start everything

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

## Logs and operations hooks

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

## Common fixes

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

## Related pages

- [[Quick Start|Quick-Start]]
- [[Visual Tour|Visual-Tour]]
- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[Troubleshooting]]
