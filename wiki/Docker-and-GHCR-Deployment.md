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

Download the release Compose file:

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/main/docker-compose.yml
```

Create a minimal `.env` for one TrueNAS system:

```bash
cat > .env <<'EOF'
APP_PORT=8080
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest

TRUENAS_HOST=https://truenas.example.local
TRUENAS_API_KEY=replace_me
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false

SSH_ENABLED=false
EOF
```

Then pull and start:

```bash
docker compose pull
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

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

## Pick An Image Tag

For most home labs, start with:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest
```

That tracks the newest published stable image.

If you want slower, more deliberate updates, pin a release:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.18.0
```

Useful tag shapes:

| Tag | Use it when |
| --- | --- |
| `latest` | you want the newest stable published image |
| `v0.18.0` | you want an exact GitHub release tag |
| `0.18.0` | you want the same stable release without the `v` prefix |
| `dev` | you are testing the current development image and accept churn |

## Update The App

If you use `latest`:

```bash
cd /docker-local/truenas-jbod-ui
docker compose pull
docker compose up -d
```

If you pin a release:

1. edit `JBOD_UI_IMAGE` in `.env`
2. pull the new image
3. recreate the services

```bash
docker compose pull
docker compose up -d
```

See what image is currently on the host:

```bash
docker compose images
```

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

## Optional Admin Sidecar

Turn on admin when you want guided setup, storage-view editing, backup/restore,
runtime controls, or the profile builder:

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

By default the admin sidecar stops itself after `3600` seconds. Change that in
`.env` only if you intentionally want a different behavior:

```dotenv
ADMIN_AUTO_STOP_SECONDS=3600
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
