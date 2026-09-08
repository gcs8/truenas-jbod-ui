# Quick Start

This page is the fastest normal-user path to a working TrueNAS-focused Docker
install from the published image.

No repo clone is required for the basic path. You only need a Docker host, a
folder for this app, and a small `.env` file with your appliance connection.
Repo cloning and local builds are for development or advanced testing.

If you want to see the screens before installing, use
[[Visual Tour|Visual-Tour]]. If you want the service map first, use
[[Architecture and Services|Architecture-and-Services]].

## What You Need

- Docker with Docker Compose
- `curl` to download the public Compose file and run health checks
- write permission for the app folder, or `sudo` access to create and assign it
- outbound HTTPS access to GitHub and GHCR for the Compose file and image
- network and firewall access from the Docker host to the TrueNAS API
- a TrueNAS CORE or SCALE API key

## 1. Make An App Folder

Use whatever folder you normally keep Compose apps in. The examples below use
`/docker-local/truenas-jbod-ui`.

```bash
sudo mkdir -p /docker-local/truenas-jbod-ui
sudo chown "$USER":"$USER" /docker-local/truenas-jbod-ui
cd /docker-local/truenas-jbod-ui
mkdir -p config/ssh config/tls data history/backups/long-term logs
```

## 2. Download the v0.22.2 Compose file

```bash
curl -fsSL \
  -o compose.yaml \
  https://raw.githubusercontent.com/gcs8/truenas-jbod-ui/v0.22.2/docker-compose.yml
```

That Compose file runs the public image from:

```text
ghcr.io/gcs8/truenas-jbod-ui
```

Public pulls do not require `docker login`.

## 3. Create `.env`

Copy the CA certificate that signed the TrueNAS HTTPS certificate. This can be
your private CA root or the required intermediate chain:

```bash
cp /path/to/truenas-ca.pem config/tls/truenas-ca.pem
chmod 0644 config/tls/truenas-ca.pem
```

Then create `.env` for a single-system TrueNAS CORE install:

```bash
cat > .env <<'EOF'
APP_PORT=8080
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2

TRUENAS_HOST=https://truenas.example.test
TRUENAS_API_KEY=replace_me
TRUENAS_PLATFORM=core
TRUENAS_VERIFY_SSL=true
TRUENAS_TLS_CA_BUNDLE_PATH=/app/config/tls/truenas-ca.pem
TRUENAS_TLS_SERVER_NAME=truenas.example.test

SSH_ENABLED=false
EOF
```

Edit the values before starting:

- `TRUENAS_HOST` is the appliance URL, without `/api/v2.0`
- `TRUENAS_API_KEY` is for TrueNAS CORE/SCALE
- `TRUENAS_PLATFORM` is usually `core` or `scale` for a first install
- `TRUENAS_VERIFY_SSL=true` keeps certificate validation on
- `TRUENAS_TLS_CA_BUNDLE_PATH` points to the mounted CA chain
- `TRUENAS_TLS_SERVER_NAME` is the DNS name on the certificate; omit it only
  when `TRUENAS_HOST` already uses that name
- `SSH_ENABLED=false` is fine for the first boot; SSH can be added later

Start with CORE or SCALE here. Less common adapters are covered on their
platform-specific setup pages so this first-run path stays focused.

### Temporary insecure diagnostic

If certificate validation blocks first-boot diagnosis, you may set
`TRUENAS_VERIFY_SSL=false` for one short test on an isolated trusted network.
This disables server identity verification and allows interception. Do not use
it as the normal configuration. Restore `true`, install the correct CA chain,
and repeat the health check before putting the app into service.

## 4. Pull and start

```bash
docker compose pull
docker compose up -d
```

This published path pairs the v0.22.2 Compose file with the v0.22.2 image. Do
not download Compose from current `main` while running the stable v0.22.2
image. The current `main` non-root layout has different ownership requirements
and belongs to the source build below.

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

For the v0.22.2 pair, updates are the normal Compose flow:

```bash
cd /docker-local/truenas-jbod-ui
docker compose pull
docker compose up -d
```

If you pin a version, edit `JBOD_UI_IMAGE` in `.env` first:

```dotenv
JBOD_UI_IMAGE=ghcr.io/gcs8/truenas-jbod-ui:v0.22.2
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

Before starting it, read the
[Admin trust boundary](https://github.com/gcs8/truenas-jbod-ui/blob/main/docs/ADMIN_TRUST_BOUNDARY.md).
On current `main`, set `ADMIN_PUBLIC_ORIGIN` to the exact scheme, host, and port
shown in the browser for the admin UI. The admin service refuses to start when
that value is missing or malformed. The default network
mode has no application login and treats every client that can reach port
`8082` as a trusted operator. The mounted Docker socket gives the sidecar
host-level container authority. Restrict network reachability to trusted
operators. Auto-stop limits exposure; it is not authentication.

Current `main` also ships the read-UI write policy. In
`ADMIN_AUTH_MODE=network`, reads remain available but the write controls disabled
by policy include mapping, alias, import, locator, and LED changes. Set
`ADMIN_AUTH_MODE=basic`, shared credentials, and the exact `APP_PUBLIC_ORIGIN`
to enable them. Each live page starts signed out. Sign in on that page before a
write; credentials stay in page memory and clear on reload or sign-out.

Those startup, disabled-control, and in-page sign-in behaviors are current-main
behavior. The v0.22.2 image used by the published path above predates them, so
keep that release restricted to trusted networks rather than relying on those
controls.

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
- [[Generic Linux Setup|Generic-Linux-Setup]]

## Advanced: Current-main source builds

Clone the repo only if you are developing, testing branch changes, or
intentionally building the image yourself.

```bash
git clone https://github.com/gcs8/truenas-jbod-ui.git
cd truenas-jbod-ui
cp .env.example .env
cp config/config.example.yaml config/config.yaml
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001
sudo python3 scripts/prepare_nonroot_bind_mounts.py . --uid 10001 --gid 10001 --apply
```

Edit `.env` before the first start; values in `.env` override matching YAML settings.
Replace the example connection values there, or remove an environment value when
you intend `config/config.yaml` to own that setting.

```bash
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
