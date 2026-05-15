# Quick Start

This page is the fastest normal-user path to a working Docker install from the
published image.

No repo clone is required for the basic path. You only need a Docker host, a
folder for this app, and a small `.env` file with your appliance connection.
Repo cloning and local builds are for development or advanced testing.

If you want to see the screens before installing, use
[[Visual Tour|Visual-Tour]]. If you want the service map first, use
[[Architecture and Services|Architecture-and-Services]].

## What You Need

- Docker with Docker Compose
- network access from the Docker host to the storage host
- an API key or API user/password for the target platform

## 1. Make An App Folder

Use whatever folder you normally keep Compose apps in. The examples below use
`/docker-local/truenas-jbod-ui`.

```bash
sudo mkdir -p /docker-local/truenas-jbod-ui
sudo chown "$USER":"$USER" /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui
mkdir -p config/ssh data history/backups/long-term logs
```

## 2. Download The Compose File

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/main/docker-compose.yml
```

That Compose file runs the public image from:

```text
ghcr.io/gcs8/truenas-jbod-ui
```

Public pulls do not require `docker login`.

## 3. Create `.env`

For a simple single-system TrueNAS CORE install:

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

Edit the values before starting:

- `TRUENAS_HOST` is the appliance URL, without `/api/v2.0`
- `TRUENAS_API_KEY` is for TrueNAS CORE/SCALE
- `TRUENAS_PLATFORM` is usually `core` or `scale` for a first install
- `TRUENAS_VERIFY_SSL=false` is common for a lab box with a self-signed cert
- `SSH_ENABLED=false` is fine for the first boot; SSH can be added later

For Quantastor, use API user/password instead of a TrueNAS API key:

```dotenv
TRUENAS_PLATFORM=quantastor
TRUENAS_API_KEY=
TRUENAS_API_USER=replace_me
TRUENAS_API_PASSWORD=replace_me
```

## 4. Pull And Start

```bash
docker compose pull
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

Check the lightweight health endpoint:

```bash
curl http://your-docker-host:8080/livez
```

Expected shape:

```json
{"status":"ok", ...}
```

## Updates

If you use `latest`, updates are the normal Compose flow:

```bash
cd /docker-local/truenas-jbod-ui
docker compose pull
docker compose up -d
```

If you pin a version, edit `JBOD_UI_IMAGE` in `.env` first:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.18.0
```

Then run:

```bash
docker compose pull
docker compose up -d
```

## Optional: History Sidecar

The app works without history. Turn this on when you want slot-history charts,
timeline heat maps, and offline snapshot exports with history samples.

```bash
docker compose --profile history pull
docker compose --profile history up -d
```

The history sidecar listens on `127.0.0.1:8081` by default and stores its
database under:

```text
./history/history.db
```

Use [[History and Snapshot Export|History-and-Snapshot-Export]] for the
walkthrough.

## Optional: Admin UI

The admin UI is optional. Turn it on when you want guided setup, storage-view
editing, backups/restores, runtime controls, or the profile builder.

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

Use [[Admin UI and System Setup|Admin-UI-and-System-Setup]] for the walkthrough.

## Optional: Run Everything

```bash
docker compose --profile history --profile admin pull
docker compose --profile history --profile admin up -d
```

## Add SSH Later

API-only mode is the easiest first boot. SSH can add:

- better slot correlation
- richer SMART detail
- SES or `sg_ses` LED control
- Linux inventory support

When you are ready, use the setup page for your platform:

- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]
- [[TrueNAS CORE Setup|TrueNAS-CORE-Setup]]
- [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]]
- [[Quantastor Setup|Quantastor-Setup]]
- [[Generic Linux Setup|Generic-Linux-Setup]]

## Advanced: Source Builds

Clone the repo only if you are developing, testing branch changes, or
intentionally building the image yourself.

```bash
git clone https://github.com/gcs8/truenas-jbod-ui.git
cd truenas-jbod-ui
cp .env.example .env
cp config/config.example.yaml config/config.yaml
docker compose -f docker-compose.dev.yml up -d --build
```

For the normal homelab install and update path, stay with the published-image
Compose flow above.

## Where To Go Next

- use [[Visual Tour|Visual-Tour]] to recognize the main screens
- use [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]] for tag
  pinning, sidecars, and update details
- use [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]] for
  logs, syslog, metrics, and Grafana
- use [[Admin UI and System Setup|Admin-UI-and-System-Setup]] for guided setup
  and saved storage views
- use [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
  before restore, migration, or destructive maintenance work
