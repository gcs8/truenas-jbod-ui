# JBOD Enclosure UI Wiki

JBOD Enclosure UI runs off-box in Docker and shows physical disk placement,
slot details, pools, vdevs, and available health data. The main UI is read-oriented.
Identify LEDs are available only on supported, configured paths.

## Start here

1. Follow [[Quick Start|Quick-Start]] to run the published image.
2. Add a storage host:
   - [[TrueNAS CORE Setup|TrueNAS-CORE-Setup]]
   - [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]]
   - [[Quantastor Setup|Quantastor-Setup]]
   - [[Generic Linux Setup|Generic-Linux-Setup]]
3. Open `http://<docker-host>:8080` and confirm that the host inventory loads.
4. Add SSH only if you need better slot mapping, SMART detail, topology data, or
   LED control.

[[Visual Tour|Visual-Tour]] shows the interface with synthetic sample data.
[[Troubleshooting]] covers missing disks, slots, and history.

`v0.23.0` is the latest published release, dated 2026-09-08. Follow
[[Quick Start|Quick-Start]] so the Compose file and image tag stay matched. See
the [GitHub release](https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.23.0)
for release-specific details.

## What runs

The main UI listens on `:8080`. Two optional sidecars use the same container
image:

- history on `:8081` for sampled metrics, events, and snapshot export
- admin on `:8082` for setup, profiles, backup and restore, and maintenance

The app connects to storage hosts from the Docker machine. The
[public demo](https://gcs8.github.io/truenas-jbod-ui/) is a static sample and
cannot connect to your systems.

See [[Architecture and Services|Architecture-and-Services]] for the service and
storage layout.

## Operator guides

- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]
- [[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]]
- [[Heat Map Mode|Heat-Map-Mode]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Backup, Restore, and Debug Bundles|Backup-Restore-and-Debug-Bundles]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]

## Deployment and configuration

- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
- [[Advanced Configuration|Advanced-Configuration]]
- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]
- [[Operations, Logging, and Metrics|Operations-Logging-and-Metrics]]
- [[Demo and Offline Workflows|Demo-and-Offline-Workflows]]
- [[Public Demo Site|Public-Demo-Site]]
