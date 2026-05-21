# Public Demo Site

This page tracks the public, interactive demo.

A static demo artifact now exists in the source tree as `public-demo/index.html`.
The repo also has a GitHub Pages workflow that publishes the checked-in
`public-demo/` directory after static artifact checks pass.

Public demo:

- https://gcs8.github.io/truenas-jbod-ui/

For the broader comparison between public demo, local demo seed, offline
snapshot, debug bundle, and full backup, see
[[Demo and Offline Workflows|Demo-and-Offline-Workflows]].

## Can GitHub Host It?

Yes, with the right boundary.

GitHub Pages can host a static site made of HTML, CSS, and JavaScript from a
repository:

- [What is GitHub Pages?](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Creating a GitHub Pages site](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)

That makes Pages a good fit for a read-only public demo built from sanitized
sample data.

It does not make Pages a replacement for the Docker app. A Pages site cannot
run the FastAPI backend, the history/admin sidecars, SSH collection, middleware
API calls, LED actions, restore workflows, or private appliance credentials.

## What The Demo Should Be

The first useful demo should let someone explore the operator experience without
owning the same hardware:

- a static web page hosted from the project repository
- one or more scrubbed or live-derived sample systems
- physical enclosure rendering
- slot details
- storage views
- heat-map mode
- sample history timeline playback when fixture data exists
- a visible `Demo / Offline` banner
- no live host connection, no admin maintenance actions, and no write paths

The closest existing concept is [[History and Snapshot Export|History-and-Snapshot-Export]].
The offline snapshot already proves that an enclosure view can be frozen into a
self-contained browser artifact. A public demo should use that same idea, but
with sample data and a site-shaped entry point.

## What It Should Not Be

- not the real `docker compose up -d` deployment
- not a hosted copy of a private lab
- not a restore or import target
- not a way to connect GitHub Pages to a visitor's TrueNAS, Quantastor, ESXi,
  Linux, UniFi, or BMC hosts
- not a place for real serial numbers, hostnames, SSH keys, API keys, TLS trust
  bundles, or history databases

## Current Foundation

The first `0.19.0` foundation uses the existing offline snapshot exporter
instead of a separate demo viewer.

It includes:

- `scripts/build_public_demo.py`
- `public-demo/index.html`
- live-derived `TN Core` data shaped like the Supermicro CSE-946 top-loader
- one 60-bay live enclosure snapshot
- saved/virtual `4x NVMe Carrier Card` and `Boot SATADOMs` storage views
- CORE-shaped pool topology with data `raidz2` groups, pool-level `spares`, `mirror-8`
  special members, and matching empty bays
- scrubbed SMART summaries and a 7-day real-history window
- no bay selected on first load, so visitors start from the whole enclosure
- critical serial, SAS, NAA, and persistent IDs scrambled consistently across
  the sample
- snapshot-mode UI behavior, so setup/export actions are absent and live
  refresh is disabled

Local build/check commands:

```powershell
python scripts/build_public_demo.py --output public-demo/index.html
python scripts/build_public_demo.py --output public-demo/index.html --check
python scripts/check_public_demo_artifact.py public-demo
```

GitHub-hosted runners do not rebuild the demo from live source data. The Pages
workflow smoke-tests the checked-in artifact by setting
`PUBLIC_DEMO_ARTIFACT=public-demo/index.html`, runs the publishability checker,
uploads `public-demo/`, and deploys it through GitHub Pages on `main` or manual
workflow dispatch.

## Published Version

1. The local build script generates `public-demo/index.html` from scrubbed
   live-derived TN Core source data.
2. The checked-in artifact loads directly in the browser and reuses the normal
   enclosure, slot detail, storage-view, and heat-map interaction patterns.
3. Live-only controls are hidden or disabled by snapshot mode.
4. `.github/workflows/publish-public-demo.yml` checks the artifact, runs the
   static Playwright smoke, uploads `public-demo/`, and deploys with GitHub
   Pages.

Later, a local `Import demo snapshot` path could let people load a scrubbed
demo fixture in browser memory. That should stay separate from admin full
backup restore, which is a real local-stack maintenance workflow.

## Tracking

This shipped in `v0.19.0`.

The Pages publication workflow and public link are now in the source tree. The
public demo does not need a Docker image or runtime patch because it is a
static artifact.

## Related Pages

- [[Quick Start|Quick-Start]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Architecture and Services|Architecture-and-Services]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
