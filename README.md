# TrueNAS JBOD Enclosure UI

TrueNAS JBOD Enclosure UI is a Docker application for seeing disks in their
physical slots. It can show enclosure layout, disk details, storage paths,
history, and identify LEDs when the connected system supports them.

It runs on a separate Docker host. Nothing is installed on TrueNAS.

[Open the public demo](https://gcs8.github.io/truenas-jbod-ui/)

## Screenshots

The screenshots use demo data, not a live storage system.

### Enclosure overview

![60-bay enclosure overview](docs/images/screenshots/public-demo-overview.png)

### Disk history

![Disk history panel](docs/images/screenshots/public-demo-history.png)

## What it does

- Shows disks in a physical chassis layout
- Displays model, serial number, size, temperature, health, and path details
- Switches between connected systems and enclosure views
- Saves custom slot mappings and chassis layouts
- Shows pool, controller, SAS, and multipath context when available
- Runs identify LEDs on supported systems
- Adds disk history and change events with the optional history service
- Provides setup, backup, restore, and maintenance tools in the optional admin UI
- Exposes health and Prometheus-compatible metrics endpoints

## Supported systems

The built-in profiles cover hardware that has been tested with the project:

- TrueNAS CORE and SCALE
- Generic Linux storage hosts
- VMware ESXi
- OSNexus QuantaStor
- Supermicro BMC and IPMI inventory
- UniFi UNVR and UNVR Pro

Support varies by platform. Some systems provide inventory only, while others
also provide SMART data, path details, or LED control. See the
[platform guides](wiki/Home.md) for tested hardware and setup notes.

## Quick start

You need Docker Compose, the URL of your TrueNAS system, and a TrueNAS API key.

```bash
mkdir -p /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui

curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/v0.22.2/docker-compose.yml

cat > .env <<'EOF'
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2
TRUENAS_HOST=https://truenas.example.test
TRUENAS_API_KEY=replace-with-your-api-key
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=false
EOF

docker compose up -d
```

Replace the example URL and API key before starting. Use `scale` instead of
`core` when connecting to TrueNAS SCALE.

Open `http://your-docker-host:8080`.

`TRUENAS_VERIFY_SSL=false` skips certificate verification so systems with a
self-signed certificate work on the first launch. After the app is working, you
can enable certificate verification by following
[Advanced configuration](wiki/Advanced-Configuration.md).

The default setup has no login. Anyone who can reach the published port can use
the controls available in that service. Do not publish the ports directly to
the Internet. Built-in authentication and stricter browser-origin checks are
available as optional settings.

For a slower walkthrough with health checks and troubleshooting, use the
[Quick Start guide](wiki/Quick-Start.md).

## Optional services

The main UI works by itself. Start history when you want charts and saved disk
events:

```bash
docker compose --profile history up -d
```

Start the admin UI when you want guided setup, profile editing, backup and
restore, or container controls:

```bash
docker compose --profile admin up -d enclosure-admin
```

The default ports are:

- Main UI: `8080`
- History service: `8081`
- Admin UI: `8082`

## Documentation

- [Quick Start](wiki/Quick-Start.md)
- [Docker deployment](wiki/Docker-and-GHCR-Deployment.md)
- [TrueNAS CORE setup](wiki/TrueNAS-CORE-Setup.md)
- [TrueNAS SCALE setup](wiki/TrueNAS-SCALE-Setup.md)
- [SSH setup](wiki/SSH-Setup-and-Sudo.md)
- [Admin UI](wiki/Admin-UI-and-System-Setup.md)
- [Troubleshooting](wiki/Troubleshooting.md)
- [All documentation](wiki/Home.md)

## Current limits

- Hardware outside the tested profiles may need a custom layout.
- ESXi integration is read-only.
- The IPMI path is focused on tested Supermicro systems.
- Full ESXi drive details require StorCLI on the host.

## License

MIT. See [LICENSE](LICENSE).