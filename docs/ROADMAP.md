# Roadmap

This file tracks the current intended release direction after `v0.3.1`.

Older milestone notes such as [`docs/V0_2_ROADMAP.md`](./V0_2_ROADMAP.md) are
kept for history, but this file is the active planning view.

Detailed execution plans live here:

- [`docs/V0_3_X_PLAN.md`](./V0_3_X_PLAN.md)
- [`docs/V0_4_PROFILE_PLAN.md`](./V0_4_PROFILE_PLAN.md)
- [`docs/V0_5_QUANTASTOR_PLAN.md`](./V0_5_QUANTASTOR_PLAN.md)
- [`docs/PROFILE_AUTHORING.md`](./PROFILE_AUTHORING.md)

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

## Longer-Term Ideas

- broader chassis-profile sharing and import/export
- safer, more portable Linux SES control rules
- additional platforms only after the adapter and profile boundaries are proven
- optional richer topology visualization once the current compact context view
  stops being enough
