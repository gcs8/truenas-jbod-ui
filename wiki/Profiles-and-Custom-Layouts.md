# Profiles and custom layouts

A profile controls how an enclosure looks. It defines bay placement, numbering,
face style, labels, and tray orientation. It does not determine where inventory
comes from.

A profile can render any of these selector entries:

- a `Live Enclosure` discovered from a host
- a `Saved Chassis View` linked to a live enclosure
- a `Virtual Storage View` for internal disks such as NVMe carriers or SATADOMs

See [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]] for
how those entries differ.

## Built-In Profiles Right Now

- `supermicro-cse-946-top-60`
- `dell-md1280-drawer-84`
- `dell-md1280-drawer-top-42`
- `dell-md1280-drawer-bottom-42`
- `supermicro-ssg-6048r-front-24`
- `supermicro-ssg-6048r-rear-12`
- `supermicro-sys-2029gp-tr-right-nvme-2`
- `supermicro-ssg-2028r-shared-front-24`
- `supermicro-aoc-slg4-2h8m2`
- `supermicro-fat-twin-front-6`
- `supermicro-fat-twin-rear-2`
- `ubiquiti-unvr-front-4`
- `ubiquiti-unvr-pro-front-7`
- `generic-front-24-1x24`
- `generic-front-12-3x4`
- `generic-top-60-4x15`
- `generic-front-60-5x12`
- `generic-front-84-6x14`
- `generic-front-102-8x14`
- `generic-front-106-8x14`

The Dell MD1280 whole-shelf profile adds top and bottom drawer sub-views. Their
selector IDs use `{enclosure_id}::{profile_id}` with
`dell-md1280-drawer-top-42` or `dell-md1280-drawer-bottom-42` as the profile ID.

## Create a custom profile

The admin sidecar includes an `Enclosure / Profile Builder`. Start from a
built-in profile, adjust its face and bay layout, preview the result, then save
it as a custom profile.

Edit `./config/profiles.yaml` directly when you need version-controlled YAML or
a field that the builder does not expose. Inside the container, this file is
`/app/config/profiles.yaml`.

A small custom profile looks like this:

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
    slot_number_base: 0
    slot_layout:
      - [4, 5, 6, 7]
      - [0, 1, 2, 3]
    row_groups: [2, 2]
```

## Layout fields

The fields used most often are:

- `id`, `label`, `panel_title`, and `edge_label` for names
- `rows`, `columns`, `slot_layout`, and `row_groups` for bay placement
- `face_style`, `latch_edge`, and `bay_size` for appearance
- `slot_hints` for matching device or controller identifiers to bays
- `slot_number_base` for zero-based or one-based displayed bay labels

Set `slot_number_base` to `0` or `1`. If omitted, it inherits the global layout
setting.

Supported built-in `face_style` values are `generic`, `top-loader`, `drawer`,
`front-drive`, `rear-drive`, `unifi-drive`, and `nvme-carrier`. An unknown value
uses the generic shape.

## Set slot order

The builder can generate bottom-up or top-down numbering by rows or columns. Use
`Custom Matrix` when the hardware follows another order. For example:

```text
02 05
01 04
00 03
```

saves as:

```yaml
slot_layout:
  - [2, 5]
  - [1, 4]
  - [0, 3]
```

Use `null` for an empty cell in a sparse or gapped layout.

Set `latch_edge` to `bottom`, `right`, `top`, or `left` to match the tray-release
edge. This affects orientation only. It does not change slot identity.

## Add slot hints

Generic Linux and NVMe layouts often need `slot_hints`. Each entry ties a visual
bay to device or PCI identifiers:

```yaml
slot_hints:
  0: ["nvme0", "0000:01:00.0"]
  1: ["nvme1", "0000:02:00.0"]
```

Verify these hints against the physical chassis before relying on the displayed
slot number for service work.

## Attach profiles to systems

Use `default_profile_id` when one profile applies to the system:

```yaml
systems:
  - id: gpu-server
    label: GPU Server Linux
    default_profile_id: supermicro-sys-2029gp-tr-right-nvme-2
```

Use `enclosure_profiles` to assign profiles to specific enclosure IDs:

```yaml
systems:
  - id: offsite-scale
    label: Offsite SCALE
    enclosure_profiles:
      "5003048001c1043f": supermicro-ssg-6048r-front-24
      "500304801e977aff": supermicro-ssg-6048r-rear-12
```

Create a custom profile when no built-in profile matches the bay count,
orientation, numbering, or device hints you have verified.
