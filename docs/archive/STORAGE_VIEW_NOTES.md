# Storage View Notes

Date: 2026-04-18

This note captures the current thinking around representing multiple physical
storage areas inside a single system without pretending they are all SES
enclosures.

## Naming Direction

Recommended user-facing name:

- `Storage View`

Why:

- `Enclosure` is too narrow once a system includes things like an internal
  NVMe carrier card or a boot-device pair.
- `Storage View` feels flexible enough to cover:
  - a real front or rear SES-managed chassis face
  - an internal PCIe/NVMe carrier card
  - a small pair of boot devices
  - a future manual or vendor-specific layout

Possible implementation detail:

- the config model can still use a more technical name like
  `storage_groups` or `storage_views`
- the UI can consistently say `Storage Views`

## Core Idea

One system can have multiple storage views.

Example for the TrueNAS CORE box:

- `Front Bays`
  - main SES-managed chassis / backplane
  - LED-capable
  - auto-discovered where possible
- `Hyper M.2 Card`
  - internal ASUS Hyper M.2 x16 carrier
  - 4 fixed slots
  - no SES / no LED
  - should still be visible as a physical layout
- `Boot SATADOMs`
  - two internal SATADOM devices on the motherboard
  - likely boot-pool only
  - probably admin-visible by default and hidden in the main read view unless
    explicitly enabled

This keeps the physical-storage story coherent without forcing everything into
the same enclosure assumptions.

## Initial Storage View Types

First-pass storage view kinds:

- `SES Enclosure`
  - real backplane / JBOD / expander
  - auto-discovered
  - LED identify support when available
- `NVMe Carrier`
  - fixed manual slot count
  - intended for cards like the ASUS Hyper M.2 x16
  - typically no LED support
- `Boot Devices`
  - small 2-slot or similar layout
  - intended for SATADOM, mirrored SATA SSD boot pairs, etc.
  - usually maintenance-oriented rather than front-page inventory
- `Generic Manual View`
  - fallback for odd internal layouts or vendor-specific cards

## UI Workflow

Suggested admin-side workflow:

1. Add a `Storage Views` section to the system setup/admin page.
2. Let the operator click `Add Storage View`.
3. Ask for a view type:
   - `Discovered SES Enclosure`
   - `NVMe Carrier`
   - `Boot Devices`
   - `Custom Manual View`
4. Ask how the view should bind:
   - auto-discover from host
   - bind by pool
   - bind by serials
   - bind by device class / transport
5. Show a preview:
   - slot labels
   - matched disks
   - LED support or not
   - whether it appears in the main read UI, admin only, or both
6. Save it into the system config.

## Rendering / Visibility Rules

Not every storage view needs equal prominence.

Recommended behavior:

- main chassis / SES views:
  - visible in the primary read UI
- internal NVMe carrier views:
  - visible in read UI when useful, especially if they host data pools
- boot-device views:
  - collapsed, hidden, or admin-only by default

Useful per-view options:

- `visible_in_main_ui`
- `visible_in_admin_ui`
- `default_collapsed`
- `supports_led`
- `supports_auto_discovery`
- `bind_mode`

## Live Enclosures Vs Saved Views

The runtime UI now needs to treat live enclosures and saved storage views as
related but distinct first-class citizens.

Current intent:

- `Live Enclosure`
  - whatever the host actually discovers right now from API and/or SSH
  - example: `LSI-F SAS3x48Front 0c04 + LSI-R SAS3x48Rear 0c04`
- `Saved View`
  - an operator-defined layout or wrapper that may sit on top of the current
    live enclosure
  - example: `Primary Chassis`, `4x NVMe Carrier Card`, `Boot SATADOMs`

Important nuance:

- `Primary Chassis` is not supposed to be a second physical shelf.
- It is a saved SES-style view layered on top of the currently selected live
  enclosure and rendered through the chosen enclosure profile.
- The UI should make that inheritance obvious enough that operators do not read
  it as a duplicate 60-bay box.

Selector guidance:

- the main UI `Enclosure / View` control should list both kinds together
- live entries should be labeled as live enclosures
- saved entries should be labeled as saved views
- maintenance-oriented views like `Boot SATADOMs` can still appear there, but
  should be clearly marked as maintenance-only rather than silently hidden

## Refresh Model

If storage views exist, refresh can become more precise:

- refresh whole system
- refresh one storage view
- refresh one pool
- rediscover SES views
- rediscover internal/manual views

This should help avoid broken state after hardware changes such as:

- replacing a JBOD and getting a new `/dev/sg*` assignment
- moving disks between front bays and an internal card
- adding or removing an internal boot or NVMe view later

## Hardware Examples To Support

### ASUS Hyper M.2 x16 Card

Observed use case:

- internal PCIe x16 card
- 4-slot NVMe carrier layout
- good fit for a reusable `NVMe Carrier` storage view template

Suggested first-pass template:

- 4 slots
- labels like `M2-1` through `M2-4`
- optional orientation metadata
- no SES / no LED assumptions

### SATADOM Pair

Observed use case:

- two SATADOMs on the motherboard
- boot pool on the CORE system

Suggested first-pass template:

- 2 slots
- labels like `DOM-A` and `DOM-B`
- hidden or collapsed in the main read UI by default
- shown clearly in admin / maintenance context

### Future AOC-SLG4-2H8M2

The user also called out a future system using:

- `AOC-SLG4-2H8M2`
- reference PDF: `C:\Users\gcs8\Downloads\AOC-SLG4-2H8M2.pdf`
- current note: ESXi host, but the card layout itself is reusable/common

Design implication:

- treat this as another likely `NVMe Carrier` style template
- keep the hardware-template layer separate from the platform adapter, so the
  same physical layout can be reused later on ESXi or Linux-style inventory
  paths

## Recommendation

The UI should move toward `Storage Views` instead of making `Enclosure` carry
all meanings.

The likely implementation order:

1. add storage views to the system config model
2. support manual/internal views first
3. add templates for:
   - ASUS Hyper M.2 x16 (4-slot)
   - SATADOM pair (2-slot)
4. keep SES discovery as one storage-view kind rather than the only kind
5. later add more reusable internal-card templates such as `AOC-SLG4-2H8M2`

## Open Questions

- Should `Storage View` be purely a UI/presentation term, with
  `storage_groups` underneath?
- Should boot-device views appear in the main UI at all, or stay admin-only by
  default?
- Should internal-card views support photo-backed previews or only abstract
  templates at first?
- How should pool-scoped refresh interact with a storage view that spans
  multiple pools or mixed-purpose disks?
