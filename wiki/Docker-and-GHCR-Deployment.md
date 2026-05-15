# Docker And GHCR Deployment

This page is the dedicated guide for running the app from published container
images instead of building from source.

Use this path when you want:

- a faster first deploy
- a simpler update flow
- the same image shape for the main UI, history sidecar, and admin sidecar
- the normal runtime path for the main UI alone or the optional sidecars

The published image lives at:

- `ghcr.io/gcs8/truenas-jbod-ui`

This package is public, so normal pulls do not require `docker login`.

## When To Use This Instead Of Local Builds

Choose the GHCR path if you want to:

- pull a tagged release image such as `v0.18.0`
- keep a server on a pinned known-good image
- update with `docker compose pull` instead of rebuilding locally

Choose the local build path if you are:

- editing the app itself
- testing unmerged branch changes
- changing Dockerfile or dependency behavior

## Files You Still Need Locally

The published image does not remove the need for local config and persistent
data folders.

Create or keep these paths beside your Compose file:

```text
./config
./config/ssh
./data
./history
./logs
```

On Linux, this creates the full directory tree the UI, history sidecar, and
admin sidecar expect to find:

```bash
mkdir -p config config/ssh data history/backups/long-term logs
```

That is safe to run even if some of the directories already exist.

For a no-clone install, download the release Compose file into that folder:

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/main/docker-compose.yml
```

Then create `.env` with your appliance connection. For a simple single-system
install, `.env` can carry the first connection by itself; `config/config.yaml`
and `config/profiles.yaml` can stay absent until you need saved multi-system
config, custom profiles, or admin-managed storage views.

## Compose File

Use the release-oriented Compose file:

```bash
docker compose up -d
```

That file uses the same published image for:

- `enclosure-ui`
- `enclosure-history`
- `enclosure-admin`

The services still split by command and environment, but they all come from the
same image. The history and admin sidecars are optional services, not dev-only
ones, so the default `docker-compose.yml` is the first-class path for all three
runtime roles.

## Optional Operations Hooks

The deployment can also expose logs, syslog, Prometheus/OpenMetrics endpoints,
and starter Grafana dashboards.

Those are day-two operations details, so they live on their own page now:

- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]

The most common knobs are:

```dotenv
LOG_FORMAT=json
METRICS_ENABLED=true
METRICS_PATH=/metrics
HISTORY_BIND_ADDRESS=127.0.0.1
```

## Pick A Tag

If you do not set anything, the default `docker-compose.yml` defaults to:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:latest
```

You can pin a specific image in `.env`:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.18.0
```

Useful tag shapes:

- `latest`
  Best for people who want the newest published stable image
- `v0.18.0`
  Best when you want the exact release-tag name from the repo
- `0.18.0`
  Equivalent stable version tag without the `v`
- `dev`
  Useful for testing the current published development image; do not treat it
  as stable

## Start The Main UI

```bash
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

Health check:

```bash
curl http://your-docker-host:8080/livez
```

For the cached dependency-status view, use:

```bash
curl http://your-docker-host:8080/healthz
```

## Optional History Sidecar

```bash
docker compose --profile history up -d
```

By default this keeps:

- the live history DB under `./history/history.db`
- rotating backups under `./history/backups`
- promoted weekly/monthly copies under `./history/backups/long-term`

The main UI will surface the `History` view when this service is healthy.

## Optional Admin Sidecar

```bash
docker compose --profile admin up -d enclosure-admin
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

This is the normal published-image path for the admin service. You only need
`docker-compose.dev.yml` if you are intentionally building from source.

## Start Everything Together

If you want the main UI plus both optional sidecars:

```bash
docker compose --profile history --profile admin up -d
```

## Update To A New Published Image

If you keep `latest`:

```bash
docker compose pull
docker compose up -d
```

If you pin a specific tag:

1. change `JBOD_UI_IMAGE` in `.env`
2. pull the new image
3. restart the services

Example:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.18.0
```

```bash
docker compose pull
docker compose up -d
```

## Switch Back To Source Builds

If you want to go back to local builds later, use the dev Compose file:

```bash
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml --profile history up -d --build
docker compose -f docker-compose.dev.yml --profile admin up -d --build enclosure-admin
```

## Troubleshooting Notes

- If `docker compose up -d` complains about missing
  `.env` values, you still need the same local `.env` and `config/config.yaml`
  setup as the source-build path.
- If the app starts but cannot talk to your appliance, the likely issue is
  config or network reachability, not GHCR itself.
- If you want the exact image currently on the host, use:

  ```bash
  docker compose images
  ```

- If you want to refresh only after a new image exists upstream, use:

  ```bash
  docker compose pull
  docker compose up -d
  ```

## Related Pages

- [[Quick Start|Quick-Start]]
- [[Architecture and Services|Architecture-and-Services]]
- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Public Demo Site|Public-Demo-Site]]
- [[Troubleshooting]]
