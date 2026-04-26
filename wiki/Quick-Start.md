# Quick Start

This page is the fastest path to a working app.

It assumes:

- you have Docker and Docker Compose
- you can reach the storage host over the network
- you already have the right API credential for the target platform

## 1. Clone The Repo

```bash
git clone <your-repo-url> truenas-jbod-ui
cd truenas-jbod-ui
```

## 2. Make The Runtime Folders

```bash
mkdir -p config config/ssh data logs
```

## 3. Copy The Example Files

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
cp config/profiles.example.yaml config/profiles.yaml
```

If you do not need custom profiles yet, `config/profiles.yaml` can stay absent.

## 4. Fill In The Basics

Edit `.env`:

- `TRUENAS_HOST`
- `TRUENAS_PLATFORM`
- `SSH_ENABLED`
- `SSH_HOST`

Credential note:

- TrueNAS CORE/SCALE use `TRUENAS_API_KEY`
- Quantastor uses `TRUENAS_API_USER` and `TRUENAS_API_PASSWORD`

For a simple single-system CORE setup, the minimum useful values usually look like:

```dotenv
APP_PORT=8080
TRUENAS_HOST=https://truenas.example.local
TRUENAS_API_KEY=replace_me
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false
SSH_ENABLED=false
```

## 5. Start The App

Choose one path:

- published GHCR image, no local build:

  ```bash
  docker compose -f docker-compose.ghcr.yml up -d
  ```

- local source build:

  ```bash
  docker compose up -d --build
  ```

If you want to pin a specific published image tag instead of `latest`, set this
in `.env` before you start:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.14.0
```

If you want the fuller published-image walkthrough, including update commands,
stable-vs-dev tag guidance, and running all sidecars from GHCR, use:

- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]

## 6. Open It

```text
http://your-docker-host:8080
```

## 7. Check Health

```bash
curl http://your-docker-host:8080/livez
```

Expected for the lightweight container health path:

```json
{"status":"ok", ...}
```

If you want the cached dependency/readiness view too:

```bash
curl http://your-docker-host:8080/healthz
```

## 8. Add SSH Later If You Want Better Data

The app works in API-only mode, but SSH can add:

- better slot correlation
- richer SMART detail
- SES or `sg_ses` LED control
- Linux inventory support

Use these pages when you are ready:

- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]
- [[TrueNAS CORE Setup|TrueNAS-CORE-Setup]]
- [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]]
- [[Quantastor Setup|Quantastor-Setup]]
- [[Generic Linux Setup|Generic-Linux-Setup]]

## 9. Optional: Turn On The Admin UI

If you want the guided setup, runtime control, backup/restore, storage-view
editing flow, or the dedicated custom-profile builder workspace, start the
optional admin sidecar:

```bash
docker compose -f docker-compose.ghcr.yml --profile admin up -d enclosure-admin
```

Or from source:

```bash
docker compose --profile admin up -d --build enclosure-admin
```

Then open:

```text
http://your-docker-host:8082
```

Use this page for the walkthrough:

- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]

## 10. Optional: Turn On History And Snapshot Export

If you want historical slot lookback and the offline HTML snapshot export flow,
start the optional history sidecar:

```bash
docker compose -f docker-compose.ghcr.yml --profile history up -d
```

Or from source:

```bash
docker compose --profile history up -d --build
```

By default that stores the live history DB under `./history/history.db`, keeps
short-term rotating backups under `./history/backups`, and promotes weekly plus
monthly long-term copies under `./history/backups/long-term`.

Then use:

- [[History and Snapshot Export|History-and-Snapshot-Export]]
