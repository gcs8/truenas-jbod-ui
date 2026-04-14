# Enclosure Profile Authoring

This guide documents the first-pass enclosure profile system introduced for
`0.4.0`.

Profiles let operators describe physical enclosure presentation without editing
Python, JavaScript, or CSS.

Use built-in profiles when they match validated hardware. Use a custom profile
file when you need a different chassis layout or want to force a specific
visual presentation for an enclosure.

## Where Profiles Live

The app loads custom profiles from:

- `paths.profile_file` in `config/config.yaml`
- or `PATH_PROFILE_FILE` in `.env`

Default path:

- `/app/config/profiles.yaml`

Example file:

- [`config/profiles.example.yaml`](../config/profiles.example.yaml)

## Minimal Schema

Each profile supports:

- `id`: stable unique identifier
- `label`: display name
- `rows`: number of rendered rows
- `columns`: number of rendered columns

Optional fields:

- `eyebrow`: small header label shown at the top of the page
- `summary`: short chassis summary shown below the title
- `panel_title`: enclosure panel title
- `edge_label`: label rendered along the enclosure edge
- `face_style`: one of:
  - `generic`
  - `top-loader`
  - `front-drive`
  - `rear-drive`
- `latch_edge`: tray-release edge used for visual polish:
  - `bottom`
  - `right`
  - `top`
  - `left`
- `slot_layout`: explicit row-by-row slot ordering
- `row_groups`: per-row grouping hints used to render divider blocks
- `slot_hints`: per-slot matching hints for SSH-only or generic Linux systems
  that need controller, device, or transport-address correlation

## Example

```yaml
profiles:
  - id: custom-lab-front-8
    label: Custom Lab Front 8
    eyebrow: Custom LAB / Front View
    summary: Operator-defined front-drive layout loaded from profiles.yaml.
    panel_title: Lab Front 8 Bay
    edge_label: Front of chassis
    face_style: front-drive
    latch_edge: right
    rows: 2
    columns: 4
    slot_layout:
      - [4, 5, 6, 7]
      - [0, 1, 2, 3]
    row_groups: [2, 2]
    slot_hints:
      0: ["nvme0", "0000:01:00.0"]
      1: ["nvme1", "0000:02:00.0"]
```

## Slot Layout Rules

`slot_layout` is a list of rendered rows from top to bottom.

Example:

```yaml
slot_layout:
  - [5, 11, 17, 23]
  - [4, 10, 16, 22]
  - [3, 9, 15, 21]
  - [2, 8, 14, 20]
  - [1, 7, 13, 19]
  - [0, 6, 12, 18]
```

That means:

- top rendered row contains slots `5, 11, 17, 23`
- bottom rendered row contains slots `0, 6, 12, 18`

If `slot_layout` is omitted, the app builds a default top-to-bottom grid based
on `rows * columns`.

## Tray Latch Orientation

`latch_edge` is optional.

Use it to match the visible release-tab edge for the enclosure profile:

- `bottom`: vertical trays with the release tab on the bottom
- `right`: horizontal trays with the release tab on the right
- `top`: vertical trays with the release tab on the top
- `left`: available for unusual left-latch layouts

Examples from the validated profiles:

- the CORE `60`-bay top-loader uses `bottom`
- the SCALE front `24` and rear `12` profiles use `right`
- the Linux GPU-server NVMe profile uses `bottom`

## Row Group Rules

`row_groups` is optional.

Use it when a row should render with visible divider groups.

Example:

```yaml
row_groups: [6, 6, 3]
```

That means a 15-slot row should visually render as:

- first 6 bays
- second 6 bays
- last 3 bays

If `row_groups` is omitted or invalid, the row renders as one continuous group.

## Attaching Profiles To Systems

You can set:

- `default_profile_id` on a system
- `enclosure_profiles` for enclosure-specific overrides

Example:

```yaml
systems:
  - id: offsite-scale
    label: Offsite SCALE
    default_profile_id: supermicro-ssg-6048r-front-24
    enclosure_profiles:
      "5003048001c1043f": supermicro-ssg-6048r-front-24
      "500304801e977aff": supermicro-ssg-6048r-rear-12
```

The selection order is:

1. enclosure-specific override
2. enclosure profile id carried by parsed inventory data
3. system default profile
4. built-in platform fallback
5. runtime inferred profile from current geometry

## Good Practices

- Keep `id` stable once mappings exist
- Treat `slot_layout` as physical truth, not storage topology
- Use `row_groups` only for visual dividers
- Use `slot_hints` when a host is inventory-only and needs extra help matching
  physical slots to controller names, namespace devices, PCI addresses, or SES
  hints
- Prefer built-in validated profiles when available
- Add notes in repo docs when a custom profile has been physically validated

## Current Built-In Profiles

- `supermicro-cse-946-top-60`
- `supermicro-ssg-6048r-front-24`
- `supermicro-ssg-6048r-rear-12`
- `supermicro-sys-2029gp-tr-right-nvme-2`

## Limitations

- Profiles describe physical presentation only
- They do not create new inventory adapters
- They do not automatically infer LED behavior
- Mapping accuracy still depends on the underlying API/SSH data being good
