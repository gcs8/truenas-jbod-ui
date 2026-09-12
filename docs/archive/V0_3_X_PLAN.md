# v0.3.x Plan

This file is the execution plan for the `0.3.x` line after `v0.3.1`.

Use this file as the working source of truth for what remains in the current
CORE/SCALE hardening cycle. If chat context gets noisy, resume from here rather
than from memory.

## Release Goal

Make the currently supported TrueNAS CORE and TrueNAS SCALE paths feel as
aligned, trustworthy, and operator-friendly as practical without taking on a
new storage platform or a broad new rendering system.

## Non-Goals

- Generic chassis/profile abstraction
- New storage platform adapters
- Deep analytics or fleet-management features
- Broad “supports everything” claims

## Current Baseline

Already in:

- CORE support for the validated `60`-bay CSE-946-style top-loading shelf
- SCALE support for the validated `SSG-6048R-E1CR36L` front `24` / rear `12`
  chassis layout
- Multi-system and multi-enclosure selection
- CORE and SCALE identify LED control through SSH fallback paths where needed
- SMART summaries on both platforms, including SCALE SSH `smartctl` fallback
- Multipath awareness on CORE
- Persistent-ID labeling (`GPTID`, `PARTUUID`, `WWN`)

## Status Snapshot

As of the current working branch, the `0.3.x` execution plan is effectively
complete:

- CORE and SCALE slot detail parity has been substantially tightened
- validated mapping and identify LED flows are working on the known hardware
- SMART, transport, persistent-ID, and multipath/operator detail are in place
- screenshots, setup notes, and regression coverage have been refreshed

Remaining `0.3.x` work should be bug-fix sized rather than another broad
feature sweep.

## Priority Order

1. CORE / SCALE field parity
2. Mapping and LED hardening
3. Setup/docs reproducibility
4. Release-prep cleanup

## Workstreams

### 1. Slot Detail Parity

Goal:

- make the detail pane feel consistent across CORE and SCALE where the
  underlying host can safely provide the same operator value

Targets:

- keep common fields aligned:
  - model
  - serial
  - size
  - persistent identifier
  - pool / class / vdev / topology
  - SMART temperature
  - last SMART test
  - power-on age
  - sector sizes
- keep transport-detail behavior predictable:
  - CORE should surface cache / SAS / link-rate data when recoverable through
    API text or SSH fallback
  - SCALE should surface the same class of data when recoverable through
    `smartctl -x -j`
- remove misleading blanks where a field is genuinely not applicable

Done criteria:

- CORE and SCALE selected-slot screenshots look materially similar
- each platform clearly distinguishes `unavailable` from `not applicable`
- no field silently disappears because a secondary enrichment source failed

### 2. Mapping And LED Hardening

Goal:

- make slot selection, mapping, and identify workflows feel dependable on the
  validated hardware

Targets:

- recheck CORE slot-to-vdev and SCALE front/rear slot correlation logic against
  real hardware notes
- keep identify LED actions narrow:
  - `IDENTIFY`
  - `CLEAR`
- reduce backend ambiguity where the same physical slot can be seen through
  multiple SES paths
- keep calibration/export/import flow stable across systems and enclosures

Done criteria:

- no validated slot layout is known to be reversed or mis-ordered
- LED actions use the correct backend for the selected platform
- mapping export/import preserves system and enclosure scope cleanly

### 3. Multipath / Transport Polish

Goal:

- keep multipath/operator context useful without turning the UI into a host
  debugging console

Targets:

- CORE:
  - preserve active/passive/fail path state
  - keep controller/HBA labels when `camcontrol devlist -v` is available
- SCALE:
  - keep SAS/LUN/link-rate detail readable
  - only expose link/transport wording that is stable enough to trust
- improve wording where raw command output is too cryptic

Done criteria:

- operator can quickly answer:
  - “which path is active?”
  - “which controller is it on?”
  - “is this disk degraded or just passive?”

### 4. Regression Safety

Goal:

- stop parser and merge regressions before they leak into screenshots or
  releases

Targets:

- expand parser tests when a real bug is found
- add inventory tests for:
  - SMART merge behavior
  - persistent-ID resolution
  - SCALE slot correlation edge cases
- keep screenshot generation reproducible

Done criteria:

- every bug fixed during `0.3.x` leaves behind a focused regression test where
  practical
- release screenshots can be regenerated with a documented script/workflow

### 5. Docs / Release Hygiene

Goal:

- keep the repo accurate enough that a future restart can happen from files
  instead of chat

Targets:

- keep README, changelog, setup examples, and SCALE notes aligned with live
  behavior
- document any sudo/SSH assumptions that are now required on validated systems
- keep screenshots current for both CORE and SCALE

Done criteria:

- no release ships with stale screenshots or obviously stale setup wording
- current validated systems and caveats are explicit

## Suggested Patch Releases

### v0.3.2

Focus:

- parity cleanup
- missing CORE/SCALE detail fields
- screenshot/doc correctness

### v0.3.3

Focus:

- mapping / LED hardening
- rear-layout sanity checks
- additional regression tests

### v0.3.4+

Focus:

- only if needed for bug fixes or release-hardening

## Exit Criteria For Moving To 0.4.0

Move on when:

- CORE and SCALE both feel operationally trustworthy on the validated hardware
- known doc drift is closed
- the remaining pain is mostly about chassis-specific rendering assumptions,
  not platform API/SSH instability
