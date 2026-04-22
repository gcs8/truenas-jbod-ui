# Profiles and Custom Layouts

The app now uses a profile-driven enclosure layout system.

That means the app can now describe:

- built-in validated chassis
- custom operator layouts
- different front/rear wording
- different tray-release orientation

without creating a new one-off render path every time.

## Profiles Are Not Storage Views

Profiles define how a chassis should look.

They do not decide whether something is:

- a `Live Enclosure` discovered from the host
- a `Saved Chassis View` that mirrors a live enclosure
- a `Virtual Storage View` for internal disks such as NVMe carriers or SATADOMs

If you want the runtime selector mental model, use:

- [[Live Enclosures and Storage Views|Live-Enclosures-and-Storage-Views]]

## Built-In Profiles Right Now

- `supermicro-cse-946-top-60`
- `supermicro-ssg-6048r-front-24`
- `supermicro-ssg-6048r-rear-12`
- `supermicro-sys-2029gp-tr-right-nvme-2`

## Where Custom Profiles Live

By default:

```text
/app/config/profiles.yaml
```

On the Docker host, that is usually:

```text
./config/profiles.yaml
```

## Builder Mode

You no longer have to start with hand-editing YAML.

The optional admin sidecar now includes a dedicated
`Enclosure / Profile Builder` workspace that can:

- load a built-in profile from the catalog
- clone it into a reusable custom profile
- adjust the face style, latch edge, bay count, and row groups
- generate common slot-ordering patterns
- save an explicit custom row matrix into `slot_layout`

![Builder workspace previewing a custom profile](images/builder-workspace-v0.13.0.png)

This is the recommended first pass for normal operator changes.

Hand-editing `profiles.yaml` is still useful when:

- you want to keep the file under your own version control
- you need fields the current builder does not expose yet
- you are experimenting with sparse/gapped layouts or other later-work schema
  details

## Example Custom Profile

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
```

## Most Useful Fields

- `id`
- `label`
- `panel_title`
- `edge_label`
- `face_style`
- `latch_edge`
- `bay_size`
- `rows`
- `columns`
- `slot_layout`
- `row_groups`
- `slot_hints`

## Slot Ordering In Builder Mode

The builder can now generate common numbering patterns without editing YAML
directly.

Examples:

- `Bottom-Up By Rows`
- `Top-Down By Rows`
- `Bottom-Up By Columns`
- `Top-Down By Columns`

If none of those match the real hardware, switch the builder to
`Custom Matrix` and enter the rows yourself.

Example:

```text
02 05
01 04
00 03
```

That saves as:

```yaml
slot_layout:
  - [2, 5]
  - [1, 4]
  - [0, 3]
```

## `latch_edge`

Use `latch_edge` to match the tray-release edge:

- `bottom`: vertical tray with release on bottom
- `right`: horizontal tray with release on right
- `top`: vertical tray with release on top
- `left`: uncommon left-latch layouts

Examples:

- CORE `60`-bay top-loader: `bottom`
- SCALE front `24` and rear `12`: `right`
- SYS-2029GP-TR right NVMe profile: `bottom`

## `slot_hints`

`slot_hints` matter most on generic Linux.

They tell the app how to correlate a visual slot with real device/controller
identifiers such as:

- `nvme0`
- `nvme1`
- PCI addresses like `0000:01:00.0`

Example:

```yaml
slot_hints:
  0: ["nvme0", "0000:01:00.0"]
  1: ["nvme1", "0000:02:00.0"]
```

## Attaching A Profile To A System

Use `default_profile_id` when one profile should usually win:

```yaml
systems:
  - id: gpu-server
    label: GPU Server Linux
    default_profile_id: supermicro-sys-2029gp-tr-right-nvme-2
```

Use `enclosure_profiles` when a system has multiple enclosure IDs:

```yaml
systems:
  - id: offsite-scale
    label: Offsite SCALE
    enclosure_profiles:
      "5003048001c1043f": supermicro-ssg-6048r-front-24
      "500304801e977aff": supermicro-ssg-6048r-rear-12
```

## When To Make A Custom Profile

Make one when:

- you have a chassis the app does not know yet
- you want a different visual orientation
- you need explicit `slot_hints`
- you are validating a future built-in profile

If you want deeper schema detail, use the repo’s longer authoring doc too.
