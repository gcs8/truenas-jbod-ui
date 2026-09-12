# Storage View Config Sketch

Date: 2026-04-18

This file sketches a likely persisted config shape for `storage_views`.

Important:

- this started as a design sketch
- the admin-first implementation now uses a close variant of this shape
- the goal is to keep the saved YAML lean and avoid storing fields that can be
  derived from `kind` or `template_id`

Related planning docs:

- [`docs/STORAGE_VIEW_NOTES.md`](./STORAGE_VIEW_NOTES.md)
- [`docs/STORAGE_VIEW_PLAN.md`](./STORAGE_VIEW_PLAN.md)

## Placement In Config

Recommended placement:

- under each system entry

Meaning:

- no top-level global `storage_views`
- each system owns its own physical storage layout definitions

Recommended shape:

```yaml
default_system_id: archive-core

systems:
  - id: archive-core
    label: Archive CORE
    default_profile_id: supermicro-cse-946-top-60
    truenas:
      host: https://10.13.37.10
      api_key: ""
      platform: core
      verify_ssl: true
    ssh:
      enabled: true
      host: 10.13.37.10
      user: jbodmap
      key_path: /run/ssh/id_truenas
      known_hosts_path: /app/data/known_hosts
      strict_host_key_checking: true
      commands:
        - /sbin/glabel status
        - /usr/local/sbin/zpool status -gP
        - gmultipath list
        - sudo -n /usr/sbin/sesutil map
        - sudo -n /usr/sbin/sesutil show
    storage_views:
      - id: front-bays
        label: Front Bays
        kind: ses_enclosure
        template_id: supermicro-cse-946-top-60
        enabled: true
        order: 10
        render:
          show_in_main_ui: true
          show_in_admin_ui: true
          default_collapsed: false
        binding:
          mode: auto
          enclosure_ids: []
          pool_names: []
          serials: []

      - id: hyper-m2
        label: Hyper M.2 Card
        kind: nvme_carrier
        template_id: asus-hyper-m2-x16-4
        enabled: true
        order: 20
        render:
          show_in_main_ui: true
          show_in_admin_ui: true
          default_collapsed: false
        binding:
          mode: hybrid
          pool_names:
            - fast
          serials: []
          pcie_addresses: []
        layout_overrides:
          slot_labels:
            0: M2-1
            1: M2-2
            2: M2-3
            3: M2-4

      - id: boot-doms
        label: Boot SATADOMs
        kind: boot_devices
        template_id: satadom-pair-2
        enabled: true
        order: 30
        render:
          show_in_main_ui: false
          show_in_admin_ui: true
          default_collapsed: true
        binding:
          mode: pool
          pool_names:
            - boot-pool
          serials: []
```

## Why This Shape

This shape tries to separate:

- identity
- rendering
- matching/binding

That should make the config easier to evolve without flattening everything into
one long list of booleans.

Recommended grouping:

- top-level view identity fields
- `render` for UI-facing behavior
- `binding` for inventory matching
- `layout_overrides` only when the template needs a local override

## Recommended Persisted Fields

### Top-Level Fields

- `id`
  - stable per-system id
- `label`
  - operator-facing name
- `kind`
  - one of:
    - `ses_enclosure`
    - `nvme_carrier`
    - `boot_devices`
    - `manual`
- `template_id`
  - built-in or future custom template id
- `enabled`
  - soft on/off
- `order`
  - stable display order without relying on YAML list order alone

### `render`

Recommended persisted UI flags:

- `show_in_main_ui`
- `show_in_admin_ui`
- `default_collapsed`

Optional later additions:

- `icon`
- `badge`
- `notes`

### `binding`

Recommended persisted binding fields:

- `mode`
  - `auto`
  - `pool`
  - `serial`
  - `hybrid`
- `enclosure_ids`
  - mainly for SES-backed views
- `pool_names`
  - useful for boot or internal-card views
- `serials`
  - explicit disk membership
- `pcie_addresses`
  - optional future hint for NVMe carriers
- `device_names`
  - optional fallback only, not preferred

### `layout_overrides`

Only store this when needed.

Examples:

- custom slot labels
- explicit row/column override for a manual template
- future orientation override

## Fields That Should Probably Not Be Persisted

These are better derived from `kind` and `template_id`:

- `supports_led`
- `supports_auto_discovery`
- `slot_count`
- `rows`
- `columns`
- `default_visibility`

Reason:

- saving derived capabilities in YAML creates drift and validation headaches
- the template registry should be the source of truth for physical shape and
  capability defaults

## SES View Sketch

Recommended saved shape:

```yaml
- id: front-bays
  label: Front Bays
  kind: ses_enclosure
  template_id: supermicro-cse-946-top-60
  enabled: true
  order: 10
  render:
    show_in_main_ui: true
    show_in_admin_ui: true
    default_collapsed: false
  binding:
    mode: auto
    enclosure_ids:
      - "500304801f715f3f"
    serials: []
    pool_names: []
```

Use this when:

- the view is fundamentally driven by discovered SES or enclosure identity

## NVMe Carrier Sketch

Recommended saved shape:

```yaml
- id: hyper-m2
  label: Hyper M.2 Card
  kind: nvme_carrier
  template_id: asus-hyper-m2-x16-4
  enabled: true
  order: 20
  render:
    show_in_main_ui: true
    show_in_admin_ui: true
    default_collapsed: false
  binding:
    mode: hybrid
    pool_names:
      - fast
    serials:
      - S59ANB0K904412E
      - S59ANB0K904423J
    pcie_addresses: []
  layout_overrides:
    slot_labels:
      0: M2-1
      1: M2-2
      2: M2-3
      3: M2-4
```

Use this when:

- the card has a known physical layout
- the disks may be best matched by pool, serial, PCIe hint, or a combination

## Boot Device Sketch

Recommended saved shape:

```yaml
- id: boot-doms
  label: Boot SATADOMs
  kind: boot_devices
  template_id: satadom-pair-2
  enabled: true
  order: 30
  render:
    show_in_main_ui: false
    show_in_admin_ui: true
    default_collapsed: true
  binding:
    mode: pool
    pool_names:
      - boot-pool
    serials: []
```

Use this when:

- the devices matter for maintenance
- they should not clutter the default front-bay operator view

## Generic Manual View Sketch

Recommended saved shape:

```yaml
- id: rear-cache
  label: Rear Cache Pair
  kind: manual
  template_id: generic-manual-2
  enabled: true
  order: 40
  render:
    show_in_main_ui: true
    show_in_admin_ui: true
    default_collapsed: false
  binding:
    mode: serial
    serials:
      - ABC123
      - DEF456
  layout_overrides:
    rows: 1
    columns: 2
    slot_labels:
      0: Cache-A
      1: Cache-B
```

Use this when:

- the hardware is odd
- the operator knows the physical grouping better than the app does

## Migration / Backward Compatibility

Recommended behavior when older configs have no `storage_views`:

- load the system normally
- treat the current default enclosure/profile behavior as the implicit primary
  storage view
- only write `storage_views` back out after the operator creates or edits one
  in admin

This keeps old configs valid and avoids a forced one-shot migration.

## Template Ownership

Recommended split:

- built-in reusable templates live in code or a dedicated template file
- per-system `storage_views` only reference templates by id

Do not copy the whole template geometry into every system entry unless the user
is intentionally overriding it.

## Open Shape Questions

- Should `render` stay nested, or should those three booleans live at the top
  level for simplicity?
- Should `order` be required, or should YAML list order be treated as the
  display order until a reorder feature lands?
- Should `binding.mode: auto` allow optional fallback `serials` or keep auto
  completely clean?
- Should future custom templates live in `profiles.yaml`, a new
  `storage_views.yaml`, or inline under `layout_overrides`?
