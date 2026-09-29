# Storage View Admin UI Plan

Date: 2026-04-18

This file turns the exploratory notes in
[`docs/STORAGE_VIEW_NOTES.md`](./STORAGE_VIEW_NOTES.md) into a concrete
admin-side implementation plan.

The first admin-facing storage-view slice is now in place, and this plan
remains the reference for follow-on work.

Concrete config-shape sketch:

- [`docs/STORAGE_VIEW_CONFIG_SKETCH.md`](./STORAGE_VIEW_CONFIG_SKETCH.md)

## Goal

Add a `Storage Views` workflow to the admin sidecar so one system can define
and manage multiple physical storage layouts without forcing every layout to be
modeled as a single SES enclosure.

This should let one system represent combinations like:

- a primary SES-managed front chassis
- an internal 4-slot NVMe carrier card
- a 2-slot SATADOM or mirrored boot-device pair

## Release Intent

This should be treated as an admin-first feature.

Meaning:

- create and edit storage views in the admin sidecar first
- store the view definitions in shared config
- expose the definitions to the read UI only after the admin workflow and data
  model feel stable

## Non-Goals For First Pass

- full ESXi platform support
- automatic correlation for every internal PCIe/NVMe card on day one
- photo-driven layout authoring
- generic topology graphing
- turning boot devices into a first-class main-screen workflow by default

## User-Facing Naming

Recommended UI label:

- `Storage Views`

Recommended internal config/model name:

- `storage_views`

Why:

- the UI term is broad and operator-friendly
- the config term is explicit and easy to reason about in code

## First-Pass Supported View Types

### 1. SES Enclosure

Use when:

- the host exposes a real SES/backplane/JBOD view

Properties:

- auto-discovery allowed
- LED support possible
- can bind to an enclosure id or discovered SES target set

### 2. NVMe Carrier

Use when:

- a PCIe carrier card physically groups a known number of M.2/NVMe devices

Initial examples:

- ASUS Hyper M.2 x16
- future `AOC-SLG4-2H8M2`

Properties:

- fixed slot count
- no SES assumption
- no LED assumption
- typically bound manually by serial, namespace, PCIe hint, pool, or a blend

### 3. Boot Devices

Use when:

- the operator wants small maintenance-oriented groups like SATADOM pairs,
  mirrored SATA SSDs, or a boot mirror

Properties:

- small slot count, usually `2`
- usually hidden or collapsed in the main read UI
- visible in admin and maintenance views

### 4. Generic Manual View

Use when:

- the operator has a physical grouping that does not fit the other presets yet

Properties:

- custom slot count
- custom labels
- no assumptions about SES or LEDs

## First-Pass Data Model

Persisted YAML shape should follow the leaner sketch in
[`docs/STORAGE_VIEW_CONFIG_SKETCH.md`](./STORAGE_VIEW_CONFIG_SKETCH.md).

The example below is intentionally broader and conceptual.

Add a new per-system collection:

```yaml
systems:
  - id: archive-core
    label: Archive CORE
    truenas:
      ...
    ssh:
      ...
    storage_views:
      - id: archive-core-front
        label: Front Bays
        kind: ses_enclosure
        enabled: true
        visible_in_main_ui: true
        visible_in_admin_ui: true
        default_collapsed: false
        supports_led: true
        supports_auto_discovery: true
        bind_mode: auto
        template_id: supermicro-cse-946-top-60
        selector:
          enclosure_id: null
          ses_hint: null
      - id: archive-core-m2
        label: Hyper M.2 Card
        kind: nvme_carrier
        enabled: true
        visible_in_main_ui: true
        visible_in_admin_ui: true
        default_collapsed: false
        supports_led: false
        supports_auto_discovery: false
        bind_mode: manual
        template_id: asus-hyper-m2-x16-4
        selector:
          serials: []
          pool_names: []
      - id: archive-core-boot
        label: Boot SATADOMs
        kind: boot_devices
        enabled: true
        visible_in_main_ui: false
        visible_in_admin_ui: true
        default_collapsed: true
        supports_led: false
        supports_auto_discovery: false
        bind_mode: manual
        template_id: satadom-pair-2
        selector:
          pool_names:
            - boot-pool
```

Minimum fields:

- `id`
- `label`
- `kind`
- `enabled`
- `visible_in_main_ui`
- `visible_in_admin_ui`
- `default_collapsed`
- `supports_led`
- `supports_auto_discovery`
- `bind_mode`
- `template_id`
- `selector`

## Template Layer

Add reusable built-in storage-view templates separate from the existing
enclosure-profile registry.

First built-ins:

- `ses-auto`
  - generic discovered SES placeholder
- `asus-hyper-m2-x16-4`
  - 4-slot NVMe carrier
  - labels `M2-1` to `M2-4`
- `satadom-pair-2`
  - 2-slot boot-device pair
  - labels `DOM-A` and `DOM-B`

Later candidate:

- `aoc-slg4-2h8m2`
  - likely another 4-slot NVMe-style template
  - future reference PDF:
    `C:\Users\gcs8\Downloads\AOC-SLG4-2H8M2.pdf`

Template metadata should include:

- slot count
- slot labels
- rows / columns
- orientation
- category
- default visibility guidance
- optional artwork/reference metadata for future UI polish

## Admin UI Scope

### A. System Setup Page Changes

Add a new `Storage Views` section to the admin system form.

This section should:

- list current storage views for the selected or loaded system
- show kind, visibility, and discovery mode at a glance
- allow:
  - add
  - edit
  - duplicate
  - delete
  - reorder

### B. Add / Edit Storage View Dialog

Fields for the dialog:

- label
- kind
- template
- enabled
- visible in main UI
- visible in admin UI
- default collapsed
- bind mode

Conditional fields:

- if `SES Enclosure`
  - auto-discovery toggle
  - discovered enclosure candidate selector
  - optional enclosure id override
- if `NVMe Carrier`
  - slot count or template-driven fixed count
  - slot labels preview
  - optional serial binding list
  - optional pool binding list
- if `Boot Devices`
  - simple 2-slot template by default
  - optional `admin only` default
- if `Generic Manual View`
  - row / column count
  - slot labels
  - custom notes

### C. Preview Panel

Each storage view being edited should show a live preview.

Preview should include:

- rendered slot layout
- labels
- whether LEDs are supported
- selected bind mode
- matched disks if current system inventory can be consulted

### D. Inventory Match Summary

When enough data is available, the dialog should show:

- matched serials
- matched pools
- unmatched expected slots
- disks currently outside any named storage view

That summary is important because it will keep the operator from creating a
nice-looking view that does not actually map to real disks.

## Binding Modes

First-pass binding modes:

- `auto`
  - use discovery results from the adapter
- `pool`
  - include disks associated with one or more pools
- `serial`
  - include explicit disk serials
- `hybrid`
  - allow view-specific fallback matching, such as pool first then serial

Per-kind recommendations:

- `SES Enclosure`: `auto`
- `NVMe Carrier`: `serial` or `pool`
- `Boot Devices`: `pool` or `serial`
- `Generic Manual View`: `serial`

## Admin Service / Backend Work

### 1. Config Model

Add support for `storage_views` in:

- config load/save
- system setup models
- admin state payload

### 2. Template Registry

Add a storage-view template registry that is distinct from enclosure profiles.

Reason:

- enclosure profiles describe a single rendered face
- storage-view templates may describe internal cards or boot groups that are
  not interchangeable with SES-oriented enclosure definitions

### 3. Validation

Validation rules should enforce:

- unique storage view ids within a system
- valid kind/template pairing
- slot count consistency for fixed templates
- no LED support on kinds that cannot support it
- no empty selector for manual-only kinds unless intentionally allowed

### 4. Inventory Correlation

Add a service layer that can answer:

- what disks belong to which storage view
- which disks are unmatched
- whether a view is stale after hardware changes

This should stay conservative in first pass.

If matching is weak, the UI should say so explicitly instead of silently
pretending the mapping is correct.

## Read UI Work After Admin Stabilization

The read UI should not be changed until the admin-side data model is stable.

Once ready, expected read-side behavior:

- allow scope switching by storage view
- keep the main chassis view primary
- optionally expose internal views like `Hyper M.2 Card`
- keep boot-device views hidden or collapsed unless explicitly enabled

## Refresh Model

Storage views make more precise refresh options possible.

Later admin and read-side controls should support:

- refresh whole system
- refresh one storage view
- refresh one pool
- rediscover SES-backed views
- re-evaluate manual/internal views against current inventory

This matters for hardware drift cases like:

- SES `/dev/sg*` renumbering
- replacing a JBOD with same slots but different SES path
- moving disks between front bays and an internal card

## Delivery Phases

### Phase 1: Data Model And Admin Persistence

Deliver:

- config schema for `storage_views`
- model validation
- admin save/load support
- no read UI changes yet

Exit criteria:

- a system can save and reload storage views reliably

### Phase 2: Admin List + Add/Edit Dialog

Deliver:

- storage views section in the admin page
- add/edit/delete/reorder actions
- live template preview

Exit criteria:

- operator can define:
  - one SES view
  - one NVMe carrier view
  - one boot-device view

### Phase 3: Matching Summary

Deliver:

- inventory-aware match summary
- unmatched-disk reporting
- warnings for weak bindings

Exit criteria:

- operator can tell whether a storage view is likely correct before saving

### Phase 4: Read UI Consumption

Deliver:

- read UI can scope by storage view
- internal views can render when enabled
- boot-device visibility rules are respected

Exit criteria:

- one validated system can switch between main chassis and an internal view

## First Validated Hardware Targets

Use these as the first real examples:

### TrueNAS CORE System

- main SES chassis / front bays
- ASUS Hyper M.2 x16 card
- SATADOM boot pair

### Future System

- AOC-SLG4-2H8M2 card
- note: current host is ESXi, but the physical-card template should still be
  reusable independently from platform support

## Testing Strategy

### Unit Tests

- model validation for storage views
- template registry tests
- selector validation
- config round-trip tests

### Admin Service Tests

- state payload includes storage views
- create/update system routes preserve storage views
- template preview payloads render correctly

### UI Tests

- add storage view
- edit storage view
- delete storage view
- reorder storage views
- preview changes when type/template changes

### Later Integration Tests

- inventory-to-storage-view matching on a validated CORE fixture
- read UI switching between multiple storage views

## Scope Guardrails

To keep first pass sane:

- do not try to solve full ESXi inventory now
- do not require photo uploads to make the feature useful
- do not mix this with a broad topology-visualization rewrite
- do not promise universal auto-discovery for internal cards

## Recommended First Build Order

1. config model
2. admin state payload
3. template registry
4. admin list UI
5. add/edit modal or inline editor
6. live preview
7. inventory match summary
8. read UI consumption later

## Open Decisions Still Worth Settling

- Should `storage_views` live directly under each system, or under a future
  broader `physical_layout` block?
- Should internal-card templates reuse the existing profile renderer, or get a
  slimmer dedicated renderer first?
- Should `Boot Devices` always default to admin-only?
- Should unmatched disks be shown as a separate pseudo-view in admin?
