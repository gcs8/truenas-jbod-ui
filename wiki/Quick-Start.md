# Quick start

This guide installs the published Docker image for one TrueNAS CORE or SCALE
system. Nothing is installed on TrueNAS.

## What you need

- Docker with Docker Compose
- `curl`
- A folder you can write to, or `sudo` access to create one
- Outbound HTTPS access to GitHub and GHCR
- Network and firewall access from the Docker host to TrueNAS
- A TrueNAS API key

## 1. Create the app folder

The examples use `/docker-local/truenas-jbod-ui`. You may use another folder.

```bash
sudo mkdir -p /docker-local/truenas-jbod-ui
sudo chown "$USER":"$USER" /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui
```

## 2. Download the Compose file

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/v0.23.0/docker-compose.yml
```

## 3. Add your TrueNAS connection

Create `.env` with the URL and API key for your system:

```bash
cat > .env <<'EOF'
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.23.0
TRUENAS_HOST=https://truenas.example.test
TRUENAS_API_KEY=replace-with-your-api-key
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false
EOF
```

Change the example URL and API key. Use `scale` instead of `core` when
connecting to TrueNAS SCALE.

The first-run setting above accepts a self-signed appliance certificate. Once
the app is working, you can turn on certificate verification by following
[[Advanced Configuration|Advanced-Configuration]].

## 4. Start the app

```bash
docker compose pull
docker compose up -d
```

Open:

```text
http://your-docker-host:8080
```

Check that the service is running:

```bash
curl http://your-docker-host:8080/livez
```

The response should include `"status":"ok"`.

## Confirm the default startup

The default setup has no login. Anyone who can reach port `8080` can use the
controls shown in the main UI. The optional admin UI works the same way on port
`8082`.

Get the basic API-only startup working before adding optional security or
enrichment settings. Do not publish these ports directly to the Internet.

## Harden access after startup

Choose the controls that fit your environment after the first successful load:

- Enable Basic authentication and set exact browser origins.
- Put the app behind an HTTPS reverse proxy or use an encrypted network path.
- Restrict port reachability with firewall rules or network segmentation.
- Import the appliance CA and enable certificate verification.
- Add SSH only when you want inventory enrichment or enabled hardware controls.

Network location is not authentication. Anyone who can reach an app port can
use the controls enabled on that port unless authentication is configured. See
[[Advanced Configuration|Advanced-Configuration]] and
[[SSH Setup and Sudo|SSH-Setup-and-Sudo]] for the opt-in steps.

## Optional history

The app works without the history service. Start it when you want charts and
saved disk events:

```bash
docker compose --profile history pull
docker compose --profile history up -d
```

History listens on `127.0.0.1:8081` by default.

## Optional admin UI

Start the admin UI when you want guided system setup, custom profiles, backup
and restore, or container controls:

```bash
docker compose --profile admin pull
docker compose --profile admin up -d enclosure-admin
```

Open:

```text
http://your-docker-host:8082
```

## Update

```bash
cd /docker-local/truenas-jbod-ui
docker compose pull
docker compose up -d
```

## Stop or remove the containers

Stop the app without deleting its saved files:

```bash
docker compose down
```

Remove the app folder only when you also want to delete its configuration,
history, mappings, and backups.

## Next steps

- [[TrueNAS CORE Setup|TrueNAS-CORE-Setup]]
- [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]]
- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
- [[Troubleshooting|Troubleshooting]]