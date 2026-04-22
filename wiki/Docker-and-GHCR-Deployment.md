# Docker And GHCR Deployment

This page is the dedicated guide for running the app from published container
images instead of building from source.

Use this path when you want:

- a faster first deploy
- a simpler update flow
- the same image shape for the main UI, history sidecar, and admin sidecar

The published image lives at:

- `ghcr.io/gcs8/truenas-jbod-ui`

This package is public, so normal pulls do not require `docker login`.

## When To Use This Instead Of Local Builds

Choose the GHCR path if you want to:

- pull a tagged release image such as `v0.13.0`
- keep a server on a pinned known-good image
- update with `docker compose pull` instead of rebuilding locally

Choose the local build path if you are:

- editing the app itself
- testing unmerged branch changes
- changing Dockerfile or dependency behavior

## Files You Still Need Locally

The published image does not remove the need for local config and persistent
data folders.

Create or keep these paths beside the repo checkout:

```text
./config
./config/ssh
./data
./history
./logs
```

From the repo root on Linux, this one command creates the full directory tree
the UI, history sidecar, and admin sidecar expect to find:

```bash
mkdir -p config config/ssh data history/backups/long-term logs
```

That is safe to run even if some of the directories already exist.

Copy the usual example files first:

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
cp config/profiles.example.yaml config/profiles.yaml
```

If you do not need custom profiles yet, `config/profiles.yaml` can stay absent.

## Compose File

Use the release-oriented Compose file:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

That file uses the same published image for:

- `enclosure-ui`
- `enclosure-history`
- `enclosure-admin`

The services still split by command and environment, but they all come from the
same image.

## Pick A Tag

If you do not set anything, `docker-compose.ghcr.yml` defaults to:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest
```

You can pin a specific image in `.env`:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.13.0
```

Useful tag shapes:

- `latest`
  Best for people who want the newest published stable image
- `v0.13.0`
  Best when you want the exact release-tag name from the repo
- `0.13.0`
  Equivalent stable version tag without the `v`
- `0.14.0-dev` or `dev`
  Useful for testing current development images; do not treat these as stable

## Start The Main UI

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Open:

```text
http://your-docker-host:8080
```

Health check:

```bash
curl http://your-docker-host:8080/healthz
```

## Optional History Sidecar

```bash
docker compose -f docker-compose.ghcr.yml --profile history up -d
```

By default this keeps:

- the live history DB under `./history/history.db`
- rotating backups under `./history/backups`
- promoted weekly/monthly copies under `./history/backups/long-term`

The main UI will surface the `History` view when this sidecar is healthy.

## Optional Admin Sidecar

```bash
docker compose -f docker-compose.ghcr.yml --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

Use this when you want:

- guided system setup
- storage-view management
- backup and restore tools
- the enclosure/profile builder workspace

## Start Everything Together

If you want the main UI plus both optional sidecars:

```bash
docker compose -f docker-compose.ghcr.yml --profile history --profile admin up -d
```

## Update To A New Published Image

If you keep `latest`:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

If you pin a specific tag:

1. change `JBOD_UI_IMAGE` in `.env`
2. pull the new image
3. restart the services

Example:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.14.0
```

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

## Switch Back To Source Builds

If you want to go back to local builds later, just use the original Compose
commands:

```bash
docker compose up -d --build
docker compose --profile history up -d --build
docker compose --profile admin up -d --build enclosure-admin
```

## Troubleshooting Notes

- If `docker compose -f docker-compose.ghcr.yml up -d` complains about missing
  `.env` values, you still need the same local `.env` and `config/config.yaml`
  setup as the source-build path.
- If the app starts but cannot talk to your appliance, the likely issue is
  config or network reachability, not GHCR itself.
- If you want the exact image currently on the host, use:

  ```bash
  docker compose -f docker-compose.ghcr.yml images
  ```

- If you want to refresh only after a new image exists upstream, use:

  ```bash
  docker compose -f docker-compose.ghcr.yml pull
  docker compose -f docker-compose.ghcr.yml up -d
  ```

## Related Pages

- [[Quick Start|Quick-Start]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Troubleshooting]]
