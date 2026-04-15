# v0.5.0 Quantastor Plan

Execution plan and completion record for the first OSNexus Quantastor release
line.

## Goal

Add a first-pass Quantastor adapter that makes the app useful on real
Quantastor hardware without overcommitting to a perfect shared-face HA model in
the first cut.

## Phase 1: API Inventory Floor

Status: complete

Target:

- reach Quantastor over REST with user/password auth
- collect storage systems, disks, pools, pool devices, and HA groups
- map those into the existing inventory model
- render a profile-backed enclosure view for the first chassis

Current implementation:

- `platform: quantastor` is wired into config and client selection
- first-pass REST client exists
- storage-system views render through the built-in shared-front `24` profile
- pool/member context is shown when `storagePoolDeviceEnum` data is present
- HA presence is surfaced as a warning
- live master-node and IO-fencing-disabled warnings are surfaced from cluster
  metadata on the validated system

## Phase 2: Shared-Face Semantics

Status: complete

Target:

- understand how dual-node shared-slot hardware should read in the UI
- decide whether the app should prefer:
  - one enclosure view with node overlays
  - two logical system views over one shared face
  - or one active-node-centric view plus peer context

Open questions:

- active-owner representation
- standby or peer-node context
- saved mapping scoping
- identify LED semantics on shared slots

Current implementation:

- one physical `1 x 24` face is rendered per Quantastor storage-system view
- slot detail can now surface shared-face operator context, including:
  - presenting node
  - active pool owner
  - fence owner
  - nodes that currently report visibility into the slot
- cluster warnings now distinguish the selected node view from the current
  cluster master on validated HA systems

## Phase 3: Richer Operator Context

Status: complete

Target:

- poll for active ownership of pools or services
- expose HA and failover state more clearly
- add any useful Quantastor-native disk or enclosure detail that does not
  already fit through the current slot-summary model

Candidate data:

- active node / standby node ownership
- fence or quorum state
- richer enclosure metadata
- disk identify state

Current implementation:

- topology labels already show pool owner context such as `active on QSOSN-Right`
- cluster warnings now surface current master-node context when the appliance
  reports `isMaster`
- cluster warnings now surface IO-fencing-disabled state when the appliance
  reports `disableIoFencing`
- slot detail now includes a dedicated shared-face context view so an operator
  can see the presenting node, active owner, fence owner, and cross-node
  visibility without leaving the selected slot
- snapshot-level platform context now records the selected node label, current
  cluster master, peer-node labels, and effective IO-fencing state for the
  selected Quantastor view

## Phase 4: CLI and LED Fallbacks

Status: complete

Target:

- use Quantastor CLI where it clearly adds operator value beyond the REST path:
  - shared enclosure slot state
  - richer disk / controller context
  - identify LEDs only if a validated controller path starts accepting them

Rules:

- keep the REST path primary when it is sufficient
- add CLI only where it clearly unlocks missing operator value
- avoid broad SSH permissions until a concrete use case exists

Validated status on the current cluster:

- `qs` CLI inventory works over SSH with explicit `--server=localhost,jbodmap,...`
- the app now consumes `qs disk-list`, `qs hw-disk-list`, and
  `qs hw-enclosure-list` as an SSH enrichment overlay when Quantastor SSH is
  enabled
- `hw-disk-identify` and `hw-enclosure-slot-identify` both fail on the
  validated LSI controller path even when run directly against the local node
- equivalent REST identify methods fail the same way
- result: Quantastor LED actions should stay explicitly unsupported in the UI
  until a validated controller path accepts identify operations

## Exit Criteria For v0.5.0

- a real Quantastor system can be added to config without code changes
- the app can render a useful slot map for the first Quantastor chassis
- pool/member context is visible enough to be operator-meaningful
- shared-face operator context is visible enough that the active owner and
  fence owner are no longer hidden behind raw API or CLI payloads
- docs/examples explain the Quantastor setup path
- the current limitations around shared-face HA are explicit instead of hidden

## Release Status

`v0.5.0` shipped on `2026-04-14`.
