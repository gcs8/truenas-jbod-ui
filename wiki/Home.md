# JBOD Enclosure UI Wiki

This wiki is the practical guide for running and tuning the project.

The goal is simple:

- let a homelabber get the app running with copy-paste steps
- let an intermediate user enable richer SSH-based mapping and SMART detail
- let an advanced user tune profiles, command sets, and multi-system layouts

If you are new here, start with:

- [[Quick Start|Quick-Start]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]] if you want the
  published-image path instead of local builds

If you already know what kind of host you are targeting, jump to:

- [[TrueNAS CORE Setup|TrueNAS-CORE-Setup]]
- [[TrueNAS SCALE Setup|TrueNAS-SCALE-Setup]]
- [[Quantastor Setup|Quantastor-Setup]]
- [[Generic Linux Setup|Generic-Linux-Setup]]

If you want to control what the enclosure looks like, use:

- [[Profiles and Custom Layouts|Profiles-and-Custom-Layouts]]
- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]

If you want to really tune things, use:

- [[Admin UI and System Setup|Admin-UI-and-System-Setup]]
- [[Docker and GHCR Deployment|Docker-and-GHCR-Deployment]]
- [[Advanced Configuration|Advanced-Configuration]]
- [[History and Snapshot Export|History-and-Snapshot-Export]]
- [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
- [[SSH Setup and Sudo|SSH-Setup-and-Sudo]]

If something is wrong, use:

- [[Troubleshooting]]

If you are maintaining the GitHub wiki itself, use:

- [[Publishing the Wiki|Publishing-the-Wiki]]

## What This App Does

The app runs off-box in Docker and talks to storage hosts over:

- the TrueNAS middleware websocket API for CORE and SCALE
- optional SSH for richer disk, enclosure, and LED detail
- SSH-only inventory for supported generic Linux hosts
- generic Linux coverage is broad enough to handle appliance-style boxes such
  as UniFi UNVR when SSH works but the vendor API does not expose disk slots
- Quantastor REST plus optional SSH/`qs`/`sg_ses` enrichment for supported HA
  appliances

It gives you:

- a physical slot map
- per-slot disk detail
- pool and vdev context
- identify LED control where supported
- manual slot calibration
- multi-system selection
- profile-driven enclosure layouts
- both local-build and published-GHCR deployment paths

## Current Validated Hardware

- TrueNAS CORE on a Supermicro CSE-946 style `60`-bay top-loading shelf
- TrueNAS SCALE on a Supermicro `SSG-6048R-E1CR36L` with front `24` and rear `12`
- OSNexus Quantastor on a Supermicro `SSG-2028R-DE2CR24L` shared front `24`
- Generic Linux on a Supermicro `SYS-2029GP-TR` with a right-side `2`-bay NVMe profile
- VMware ESXi `7.0.3` on a Supermicro `AOC-SLG4-2H8M2`, using SSH `esxcli`
  plus StorCLI for a read-only `2`-slot M.2 RAID-member view
- UniFi UNVR as generic Linux over SSH with a built-in `4`-bay profile and
  validated vendor-local LED control
- UniFi UNVR Pro as generic Linux over SSH with a built-in `7`-bay `3-over-4`
  profile and experimental vendor-local LED control

## Current Direction

- `0.13.0` shipped the selectable backup/debug-bundle scope work, the demo
  builder seed path, the first embedded boot-media view for UniFi, and the
  current internal-view visual polish
- `0.14.0` rounds out the current carry-over cycle with first-pass read-only
  ESXi support on the validated `AOC-SLG4-2H8M2` path, better admin guardrails
  around non-Linux hosts, and a more responsive stale-cache-first runtime
  switching path for the already validated systems
- `0.14.1` is the narrow SSH-only setup hotfix: the admin save path now lets
  `esxi`, generic Linux, and UniFi-family systems persist cleanly when SSH is
  the real source of truth and no API host should be required first

## Visual Walkthrough

If you learn better by seeing the flow first, start here:

- [[History and Snapshot Export|History-and-Snapshot-Export]]

That page shows:

- the live history drawer on a populated slot
- the export snapshot dialog with live size estimates
- the frozen offline snapshot HTML after export
- the maintenance/recovery follow-up lives on
  [[History Maintenance and Recovery|History-Maintenance-and-Recovery]]
