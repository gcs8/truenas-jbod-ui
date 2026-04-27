# Roadmap

This file tracks the current intended release direction after `v0.16.0`.

Older milestone notes such as [`docs/V0_2_ROADMAP.md`](./V0_2_ROADMAP.md) are
kept for history, but this file is the active planning view.

Detailed execution plans live here:

- [`docs/V0_3_X_PLAN.md`](./V0_3_X_PLAN.md)
- [`docs/V0_4_PROFILE_PLAN.md`](./V0_4_PROFILE_PLAN.md)
- [`docs/V0_5_QUANTASTOR_PLAN.md`](./V0_5_QUANTASTOR_PLAN.md)
- [`docs/V0_8_HISTORY_PLAN.md`](./V0_8_HISTORY_PLAN.md)
- [`docs/V0_9_0_PLAN.md`](./V0_9_0_PLAN.md)
- [`docs/V0_11_0_PLAN.md`](./V0_11_0_PLAN.md)
- [`docs/PROFILE_AUTHORING.md`](./PROFILE_AUTHORING.md)

## Current Snapshot

`v0.16.0` just shipped the first observability/runtime-operations slice:

- optional generic syslog shipping
- shared JSON log output across UI/history/admin
- scrape-based Prometheus/OpenMetrics endpoints on all three services
- first-pass inventory/cache metrics plus checked-in Grafana dashboards
- live Windows-vs-Linux comparison strong enough to keep Linux as the primary
  perf truth and local Windows Docker Desktop as the slower tuning target

The next cycle should stay narrower than that release:

- keep Linux Docker as the primary perf truth while chasing the still-slow
  local Windows `history_status` and snapshot-export path
- decide whether the observability slice grows alert rules, richer structured
  perf-event logging, or optional host-level `node_exporter` coverage
- investigate the intermittent `unvr-pro` SSH slow path without widening the
  app's normal request path around one slow UniFi box
- confirm the remaining sibling FatTwin ESXi node behavior and live-confirm
  rear-bay numbering later once hardware is available

## Guiding Principle

Keep the app focused on:

- slot identity
- LED identify control
- physical-disk situational awareness
- practical operator workflows across a small number of validated platforms

Avoid turning it into a full storage analytics or appliance-management suite.

## v0.3.x - CORE / SCALE Parity And Hardening

Goal:

- make the currently supported TrueNAS CORE and TrueNAS SCALE paths feel as
  aligned as practical before taking on another platform

Priority areas:

- improve parity between API-backed and SSH-backed field coverage where that can
  be done safely
- keep CORE and SCALE slot details as similar as the underlying platforms allow
- harden Linux SES and SMART parsing on SCALE
- tighten docs, screenshots, and setup examples so the current workflows are
  reproducible
- keep the validated hardware set explicit instead of implying universal support

Examples of good `v0.3.x` work:

- API/SSH field parity improvements
- richer but stable SMART detail
- better enclosure wording and orientation cues
- safer LED identify workflows
- bug fixes and parser regression tests

Detailed plan:

- [`docs/V0_3_X_PLAN.md`](./V0_3_X_PLAN.md)

## v0.4.0 - Generic Chassis / Profile System

Goal:

- move enclosure rendering away from hardcoded hardware assumptions and toward a
  profile-driven layout model

Primary outcomes:

- built-in profiles for validated Supermicro chassis and shelves
- custom YAML-defined chassis layouts for operator-specific gear
- profile metadata for:
  - bay grouping
  - row / column orientation
  - front vs rear labeling
  - service-area layout
  - bay aspect ratio and spacing
  - tray latch / release-tab orientation
  - enclosure wording and edge labels

Why this comes before another platform:

- it gives us a cleaner rendering and mapping foundation
- it reduces the amount of one-off UI logic needed per appliance family
- it makes future platform adapters easier because physical presentation becomes
  more declarative

Detailed plan:

- [`docs/V0_4_PROFILE_PLAN.md`](./V0_4_PROFILE_PLAN.md)

## v0.5.0 - OSNexus Quantastor Support

Goal:

- add a new storage-platform adapter after the chassis/profile layer is in place

Primary outcomes:

- initial Quantastor inventory adapter
- topology and slot-detail mapping into the shared UI model
- reuse of the profile-driven enclosure system rather than inventing another
  hardware-specific rendering path
- decide how to represent dual-node appliances that share a single enclosure
  face, starting with the Supermicro `SSG-2028R-DE2CR24L`

Notes:

- [`docs/QUANTASTOR_NOTES.md`](./QUANTASTOR_NOTES.md)
- [`docs/V0_5_QUANTASTOR_PLAN.md`](./V0_5_QUANTASTOR_PLAN.md)

Current status:

- shipped in `v0.5.0`
- current implementation is still intentionally first-pass and
  storage-system-scoped, but it now includes shared-face HA context, optional
  CLI enrichment, verified SMART merge, and SES-aware LED fallback on the
  validated cluster

Why this is not `v0.4.0`:

- we want to avoid mixing "new platform adapter" work with "new chassis/profile
  abstraction" work in the same release

## v0.6.0 - UniFi UNVR Family And Secure Defaults

Goal:

- prove that the generic Linux path can stretch to appliance-style storage
  boxes that expose useful SSH and vendor-local tooling even when they do not
  expose SES or a rich public disk API

Primary outcomes:

- shipped `ubiquiti-unvr-front-4` and `ubiquiti-unvr-pro-front-7` built-in
  profiles
- password-SSH support plus keyboard-interactive fallback for appliance Linux
  hosts
- vendor-native UniFi slot mapping through `ubntstorage disk inspect`
- ATA/SATA SMART enrichment for the UniFi family, including cache state,
  link-rate metadata, and lifetime read/write counters when available
- validated vendor-local SSH LED control for the regular UNVR and experimental
  parity for the UNVR Pro
- safer release defaults for SSH/API trust, including TOFU-pinned SSH host keys
  and sample configs that prefer TLS verification by default

Current status:

- shipped in `v0.6.0`
- regular UNVR support is operationally validated for inventory, SMART, layout,
  and LED control
- UNVR Pro support is solid for inventory/layout/SMART and still intentionally
  experimental for LED control until more bays are confirmed on real hardware

## v0.7.0 - Layout Polish, Topology Truth, And Release Cleanup

Goal:

- make the currently validated platforms feel more intentional and better
  documented before taking on another larger adapter or workflow jump

Primary outcomes:

- bay-size-aware enclosure geometry for validated profiles
- a denser, more faithful CORE `60`-bay top-loader layout with visible divider
  rails
- Linux topology labeling that prefers practical mounted data volumes over boot
  or swap noise
- refreshed screenshots and release-facing docs for the current platform set

Current status:

- shipped in `v0.7.0`
- the layout polish, topology cleanup, and refreshed release-facing screenshots
  all landed in the release

## v0.8.0 - Optional Slot History And Snapshot Export

Goal:

- add a lightweight historical lookback path without turning the main app into
  a full monitoring suite

Primary outcomes:

- optional SQLite-backed sidecar collector in Docker Compose
- low-cadence SMART metric sampling for a short list of operator-useful fields
- change-only slot timeline for disk swaps, state drift, and pool / vdev moves
- keep the main UI safe and standalone when the sidecar is disabled
- expose slot history inside the main `:8080` UI instead of forcing operators
  onto a separate sidecar page
- keep the history store backup and inspection story simple with SQLite
  snapshots on the existing bind mount

Current notes:

- [`docs/V0_8_HISTORY_PLAN.md`](./V0_8_HISTORY_PLAN.md)

Current status:

- shipped in `v0.8.0`
- the optional history sidecar, offline snapshot export flow, and the
  screenshot/wiki walkthroughs all landed in the release

## v0.9.0 - Perf Harness And Reusable Profiles

Goal:

- use the pre-`1.0` window to add practical performance observability and a
  broader reusable profile base without turning the project into a full
  telemetry stack or a giant vendor-import exercise

Primary outcomes:

- opt-in request and workflow timing for the expensive inventory and slot paths
- a small repeatable perf harness to catch release-to-release slowdowns
- measured cleanup of obvious duplicate or wasteful work
- a batch of broadly reusable chassis / JBOD profiles derived from the
  Quantastor layout references rather than from one-off hardcoded additions

Why this comes next:

- the app already has caching and refresh knobs, but it still lacks a clean
  performance-observability story
- current mutating workflows appear likely to rebuild snapshots more than once,
  making perf cleanup a good candidate for measured low-risk wins
- the external Quantastor reference set shows substantial overlap across common
  `12`, `24`, `60`, `84`, and `106` bay shapes that can inform more reusable
  profile coverage

Current notes:

- [`docs/V0_9_0_PLAN.md`](./V0_9_0_PLAN.md)

Current status:

- shipped in `v0.9.0`
- perf instrumentation, the HTTP perf harness, browser QA, and the reusable
  generic profile batch all landed in the release
- eBPF and other native profiler ideas remain optional investigation items, not
  the required center of the milestone

## v0.10.0 - Admin Sidecar, Storage Views, And Recovery

Goal:

- turn the app into a more complete operator workflow without abandoning the
  simple read-only enclosure UI at its center

Primary outcomes:

- optional `admin` sidecar for setup, backup/restore, runtime control, SSH key
  management, and TLS trust workflows
- storage views promoted to first-class runtime targets instead of metadata-only
  side paths
- inventory-bound views such as `Boot SATADOMs` and the NVMe carrier wired into
  the normal SMART and history flows
- clearer operator wording between `Live Enclosures`, `Saved Chassis Views`,
  and `Virtual Storage Views`
- more resilient history-sidecar behavior on the validated Docker Desktop
  bind-mount path

Current status:

- shipped in `v0.10.0`
- the release tag, release notes, screenshots, and wiki refresh are complete
- the remaining follow-up work is intentionally being carried into `v0.11.0`

## v0.11.0 - Runtime Sanity, Bootstrap Clarity, And Cleanup

Goal:

- validate the new live-versus-saved runtime model on real systems, then close
  the highest-value clarity and cleanup gaps before broadening scope again

Primary outcomes:

- live-host sanity pass for the grouped selector wording and grouped admin
  `Add Storage View` flow
- remaining storage-view first-class parity fixes plus a decision on legacy
  saved `ses_enclosure` profile behavior
- either backend/docs alignment or an explicit documentation correction for the
  CORE `midclt call user.update` bootstrap story
- follow-up decision on whether the Docker Desktop history-sidecar root-user
  workaround can eventually relax
- optional Quantastor and snapshot-export cleanup if those still look like the
  best next low-risk wins afterward

Current notes:

- [`docs/V0_11_0_PLAN.md`](./V0_11_0_PLAN.md)

Current status:

- shipped in `v0.11.0`
- release-facing docs, screenshots, Quantastor HA guidance, and the external
  wiki refresh all landed with the cut
- deferred follow-ups now live in `HANDOFF.md` / `TODO.md`, with
  snapshot-export profiling and broader non-Quantastor runtime sanity at the
  top of the carry-over list

## v0.12.0 - Builder Mode, Linux Host Parity, And Release Closeout

Goal:

- finish the profile-builder foundation and the highest-value shared-geometry
  cleanup without opening a broader freeform-layout project yet

Primary outcomes:

- dedicated `Enclosure / Profile Builder` workspace in the optional admin
  sidecar
- custom profile save/update/delete backed by `profiles.yaml` and the existing
  profile registry
- slot-ordering presets plus an explicit `Custom Matrix` editor for
  `slot_layout`
- Linux host-side path cleanup so repo-local runs stop assuming a writable
  `/app/{config,data,logs,history}` harness
- saved live-backed chassis parity so the selected profile, not the runtime
  view type, owns tray, latch, LED, and row geometry
- snapshot-export estimate recovery after restoring the batched scope-history
  read path and tightening history-sidecar SQLite settings

Current status:

- shipped in `v0.12.0`
- the core product work and release-facing docs/screenshots/validation are
  complete
- a more visual tile-level builder editor is intentionally deferred to backlog
  instead of being part of this release scope

## v0.13.0 - Support Bundles, Embedded Boot Media, And Storage View Polish

Goal:

- round out the operator-support workflows around admin-side bundles and polish
  the first-pass internal-device views that now sit alongside the larger
  enclosure workflows

Primary outcomes:

- selectable admin full-backup scope with locked secret-path pills that force
  encrypted `.7z` export when SSH keys, TLS trust material, or shared
  `known_hosts` are included
- separate scrub-capable debug bundles for frozen support snapshots without
  pretending that flow is a restore path
- seeded `demo-builder-lab` system/profile/view fixtures so profile and storage
  view work can be exercised locally without a real appliance first
- optional UniFi UNVR / UNVR Pro `Embedded Boot Media` view backed by limited
  `smartctl -d scsi` detail from `/dev/boot`
- photo-backed SATADOM rendering and the small follow-up visual/detail polish
  needed to make those internal views read cleanly in the live UI

Current status:

- shipped in `v0.13.0`
- the current release-facing work centers on backup/debug/demo workflows,
  internal boot-media visibility, and tighter storage-view presentation polish
- deeper debug-bundle replay/viewer ideas and broader builder-mode editing
  remain intentionally deferred to backlog

## v0.14.0 - ESXi Read-Only Support And Read-Path Cleanup

Goal:

- add a narrow, operator-honest ESXi adapter while also reducing switch and
  refresh friction on the already validated platforms

Primary outcomes:

- first-pass read-only VMware ESXi support on the validated Supermicro
  `AOC-SLG4-2H8M2` path using SSH `esxcli` plus StorCLI JSON
- a built-in photo-backed `2`-slot AOC carrier profile/template that renders
  physical RAID members `13:0` and `13:1` directly on the board image
- admin-side setup guardrails that keep ESXi out of the Linux
  bootstrap/sudoers flow and recommend the narrower saved SSH shape instead
- stale-cache-first system/enclosure switching, lightweight `/livez`, cached
  `/healthz`, scoped cache invalidation, and a non-blocking Quantastor LED
  verify follow-up instead of forcing full blocking refreshes on page load

Current status:

- shipped in `v0.14.0`
- the current release-facing work centers on ESXi operator clarity,
  responsiveness, and refreshed README/wiki screenshots
- the later ESXi credential model question, shared `ses_enclosure` geometry
  cleanup, and Windows `history_status` tuning remain intentionally deferred

## Longer-Term Ideas

- broader chassis-profile sharing and import/export
- safer, more portable Linux SES control rules
- additional platforms only after the adapter and profile boundaries are proven
- optional richer topology visualization once the current compact context view
  stops being enough
