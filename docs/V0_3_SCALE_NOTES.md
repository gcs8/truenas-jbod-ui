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
- Linux SES enclosure-status reads through `sudo -n /usr/bin/sg_ses -p ec`
- Front `24`-bay enclosure picker and slot map from `/dev/sg27`
- Rear `12`-bay enclosure picker and slot map from `/dev/sg38`
- Disk-to-slot correlation from SCALE `lunid` values plus AES `SAS address`
- Per-slot SMART summary through `sudo -n /usr/sbin/smartctl -x -j /dev/<disk>`
- SSH identify LED control through `sg_ses --set=ident` / `--clear=ident`
- Physical front/rear slot geometry aligned to CryoStorage operator notes:
  - front = `4` columns by `6` rows, with each `6`-disk vertical column acting as one vdev and slot numbers running bottom-to-top within each column
  - rear = `4` columns by `3` rows

Not working yet:

- Detailed SMART JSON via the websocket API
- SMART test history via the websocket API

## Live Findings

### API Deprecation Watch

On `2026-04-14`, the SCALE host reported this alert:

- the deprecated REST API authenticated `3` times in the last `24` hours
- source IP reported by the alert: `10.13.37.67`
- removal target called out by the appliance alert: `26.04`

Important context for this app:

- this app talks to TrueNAS through the middleware websocket endpoint
  (`/websocket`) using `auth.login_with_api_key`
- the current codebase does not intentionally call `/api/v2.0` for TrueNAS
  inventory, SMART, enclosure, or LED actions

That means this alert is very likely coming from some other integration,
browser session, script, or tool running from the same Docker host / IP rather
than from this app itself.

Follow-up to keep in mind:

- investigate other integrations on `10.13.37.67`
- keep this app on the websocket / JSON-RPC path only
- treat REST-removal compatibility as an explicit `v0.3.x` parity and hardening
  item rather than a future cleanup

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
- `sudo -n /usr/bin/sg_ses -p ec /dev/sg27`
- `sudo -n /usr/bin/sg_ses -p ec /dev/sg38`

That means first-pass Linux SES page parsing is now practical on this host, and
the current app branch is using it to drive:

- a front `24`-slot enclosure view that is `4` columns wide by `6` rows tall, with the front-view rows flipped so slot `0` sits at the bottom and slot `5` at the top of column `1`
- a rear `12`-slot enclosure view that is `4` columns wide by `3` rows tall
- per-slot disk correlation where SCALE `disk.details` and AES page data agree
- current identify LED state through parsed enclosure-status pages

Identify LED control was then validated through:

- `sudo -n /usr/bin/sg_ses --dev-slot-num=0 --set=ident /dev/sg27`
- `sudo -n /usr/bin/sg_ses --dev-slot-num=0 --clear=ident /dev/sg27`
- `sudo -n /usr/bin/sg_ses --dev-slot-num=0 --set=ident /dev/sg38`
- `sudo -n /usr/bin/sg_ses --dev-slot-num=0 --clear=ident /dev/sg38`

That means the current app branch can now drive first-pass SCALE identify LEDs
on both the front and rear enclosures.

After `/usr/sbin/smartctl` was added to the allowed sudo commands, these
one-off SMART probes also worked as `jbodmap`:

- `sudo -n /usr/sbin/smartctl -x -j /dev/sdc`
- `sudo -n /usr/sbin/smartctl -x -j /dev/sdab`

That gives the app a practical SCALE SMART path even though the websocket API
still does not expose the same detailed SMART JSON coverage we have on CORE.
The app now uses SSH `smartctl` JSON on demand for the selected slot to show:

- current temperature
- last SMART self-test type and status
- last SMART self-test lifetime hours and relative age
- power-on hours and days
- logical and physical block size
- transport protocol
- logical unit ID
- SAS address and attached SAS address
- negotiated link rate

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
- The front visual order is already mapped in operator notes as `4` columns by `6` rows, with slot numbering running bottom-to-top per column
- The rear visual order is already mapped in operator notes as `4` columns by `3` rows
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
2. Build a SCALE-specific enclosure profile that uses the front/rear hardware notes directly
3. Add richer Linux transport detail such as SAS address, LUN id, and link-rate context where it is stable enough to trust

`sg_ses` AES plus enclosure-status parsing is now usable for mapping and first-pass
identify LEDs, so the next real question is how much more Linux device detail we
want to expose without turning this into a host-debugging UI.
