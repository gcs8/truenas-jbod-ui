# v0.3 SCALE Notes

## Target System

- Chassis: `Supermicro SSG-6048R-E1CR36L`
- Front SAS backplane: `BPN-SAS3-846EL1`
- Rear SAS backplane: `BPN-SAS3-826EL1-N4`
- Host: `10.88.88.40`
- Platform: `TrueNAS SCALE`

## First-Pass Status

The app can now treat SCALE as a selectable system alongside CORE, and it can
build enclosure maps from Linux SES AES pages even when the SCALE middleware API
does not expose enclosure rows.

Working today:

- `disk.query`
- `disk.details`
- `disk.temperatures`
- `pool.query`
- SSH connectivity as `jbodmap`
- Linux command probes such as `zpool`, `lsblk`, and `lsscsi -g`
- Linux SES AES page reads through `sudo -n /usr/bin/sg_ses`
- Front `24`-bay enclosure picker and slot map from `/dev/sg27`
- Rear `12`-bay enclosure picker and slot map from `/dev/sg38`
- Disk-to-slot correlation from SCALE `lunid` values plus AES `SAS address`

Not working yet:

- LED control
- Detailed SMART JSON via the websocket API
- SMART test history via the websocket API

## Live Findings

### API

The SCALE API on this system returns useful disk and pool inventory, but not a
usable enclosure layout for the current UI:

- `enclosure2.query` returns an empty list
- `enclosure.query` is not a reliable path on this host
- `disk.details` exposes Linux-specific hints such as:
  - `hctl`
  - `sectorsize`
  - `devname`
  - `lunid`
  - `bus`
  - `identifier`

This is enough to render a system-level disk inventory, topology context, and,
with SSH AES parsing, physical slot maps for the front and rear enclosures.

### SSH

The non-root SCALE account can successfully run:

- `/usr/sbin/zpool status -gP`
- `/usr/bin/lsblk -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,HCTL`
- `/usr/bin/lsscsi -g`

It could not initially use the Linux SES tooling needed for enclosure mapping
or LED control without additional permissions:

- `sg_ses` was present but permission denied against the enclosure device nodes
- `smartctl --scan-open` was permission denied against the device nodes

`lsscsi -g` did confirm Linux enclosure devices are present, including SES/SG
targets that likely correspond to the front and rear backplanes.

After command-limited sudo was added for `/usr/bin/sg_ses`, these probes worked
as `jbodmap`:

- `sudo -n /usr/bin/sg_ses -p aes /dev/sg27`
- `sudo -n /usr/bin/sg_ses -p aes /dev/sg38`

That means first-pass Linux SES page parsing is now practical on this host, and
the current app branch is using it to drive:

- a front `24`-slot `4 x 6` enclosure view
- a rear `12`-slot `3 x 4` enclosure view
- per-slot disk correlation where SCALE `disk.details` and AES page data agree

## Why The Hardware Info Matters

This system is not just "CORE, but different". It is a Linux enclosure problem:

- different middleware coverage
- different device naming
- different SES tooling
- different permission model

The `846EL1` front plus `826EL1-N4` rear combination strongly suggests that the
eventual SCALE enclosure implementation should target a split front/rear layout
rather than assuming a single 60-bay top-loader style shelf.

## Operator Notes That Change The Plan

The local CryoStorage notes add several concrete mapping hints that are more
useful than the current live API output:

- EID `26` appears to be the front `24`-bay enclosure on `/dev/sg27`
- EID `14` appears to be the rear `12`-bay enclosure on `/dev/sg38`
- The front visual order is already mapped in operator notes as a `6 x 4` grid
- The rear visual order is already mapped in operator notes as a `3 x 4` grid
- The current notes also carry serial, WWN, SAS address, Linux device, and SG
  device correlations for most of the installed drives

That means the next SCALE milestone probably should not aim for a fake
60-bay-style shelf. It should aim for a chassis-specific front/rear layout with
two separate enclosure views:

- front 24-bay (`EID 26`)
- rear 12-bay (`EID 14`)

This note set is also strong enough to bootstrap a profile-driven manual
mapping/import path for any slots that still need operator help.

## Likely Next Step

For the next SCALE pass, the most valuable improvement is probably one of:

1. Parse `lsblk` and `lsscsi -g` into a better Linux disk presentation layer
2. Add a real Linux LED control backend once the safe `sg_ses` control form is verified
3. Build a SCALE-specific enclosure profile that uses the front/rear hardware notes directly

`sg_ses` AES parsing is now usable for mapping, so the next real question is
how much of the Linux SES control surface we want to expose safely.
