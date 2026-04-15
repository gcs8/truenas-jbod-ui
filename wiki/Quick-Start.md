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

```bash
docker compose up -d --build
```

## 6. Open It

```text
http://your-docker-host:8080
```

## 7. Check Health

```bash
curl http://your-docker-host:8080/healthz
```

Expected:

```json
{"status":"ok","dependency_status":"ok", ...}
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
