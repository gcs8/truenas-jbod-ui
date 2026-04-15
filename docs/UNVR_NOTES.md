# UniFi UNVR Discovery Notes

Date: 2026-04-15

Release context:

- regular UNVR support ships in `v0.6.0` as validated inventory, SMART,
  profile/layout, and SSH LED control
- UNVR Pro support ships in `v0.6.0` as validated inventory, SMART, and
  profile/layout, with experimental SSH LED control

## Host Under Test

- Appliance: `unvr.gcs8.io`
- Protect integration API key auth tested
- SSH tested as `root` with password auth
- companion consistency probe: `192.168.1.174` (UNVR Pro) over the same
  password-only `root` SSH path

## Protect API Endpoints Verified

The documented Protect integration API works with the provided API key:

- `GET /proxy/protect/integration/v1/meta/info`
- `GET /proxy/protect/integration/v1/nvrs`

Live results:

- `/proxy/protect/integration/v1/meta/info` returned `{"applicationVersion":"7.0.106"}`
- `/proxy/protect/integration/v1/nvrs` returned a single NVR object with:
  - `id`
  - `modelKey = "nvr"`
  - `name = "UNVR.gcs8.io"`

This is useful for appliance identity, but it does not expose per-drive slot
inventory by itself.

## Local UniFi OS API Results

These appliance-local routes also worked:

- `GET /api/system`
- `GET /api/apps`

Observed highlights:

- `hardware.shortname = "UNVR"`
- `name = "UNVR.gcs8.io"`
- `apps = []`, `controllers = []`

These storage-looking routes did not produce usable inventory with the API key:

- `GET /api/storage/peripherals/` -> `401 Unauthorized`
- `GET /api/storage/spaces/` -> `401 Unauthorized`

## SSH Findings

The host is friendly to the generic Linux inventory path:

- `uname -a` reports `Linux ... 4.19.152-alpine-unvr`
- `/etc/os-release` reports Debian 11 userspace
- available tools:
  - `/bin/lsblk`
  - `/usr/sbin/smartctl`
  - `/usr/bin/sg_ses`
  - `/sbin/mdadm`

### Storage Layout

- four data disks: `sda`, `sdb`, `sdc`, `sdd`
- model: `ST4000NM0115-1YZ107`
- transport: `sata`
- vendor front-face artwork confirms the regular UNVR bay order is a single
  horizontal `1-4` row from left to right, which maps cleanly to the app's
  zero-based `0-3` profile
- md arrays:
  - `md0` = `raid1` boot/swap style member set
  - `md3` = `raid5` data volume

### Enclosure Discovery

- `/sys/class/enclosure` is empty
- no sysfs enclosure rows were exposed for slot mapping

### Stable Linux Hints Seen

- `sda` -> `0:0:0:0` -> `ID_PATH ... ata-1.0`
- `sdb` -> `2:0:0:0` -> `ID_PATH ... ata-3.0`
- `sdc` -> `4:0:0:0` -> `ID_PATH ... ata-1.0` on the second PCI function
- `sdd` -> `6:0:0:0` -> `ID_PATH ... ata-3.0` on the second PCI function

These look stable enough to be useful as future slot hints once physical bay
order is validated.

### Vendor Disk Inventory

`/usr/sbin/ubntstorage disk inspect` turned out to be the most useful
vendor-native inventory source on both appliances.

What it adds beyond plain `lsblk`:

- authoritative vendor bay numbering
- explicit `nodisk` rows for empty bays
- model / serial / firmware / temperature / power-on-hour context
- a cleaner on-box source for UniFi-family slot correlation than raw HCTL alone

The local app now uses that command as the primary UniFi slot source and keeps
`lsblk`/`smartctl` for Linux disk identity, pool, and SMART detail.

### SMART Detail

`smartctl -x -j` works directly on all four drives and exposes:

- model / serial / firmware
- logical + physical block size
- rotation rate
- form factor
- SATA max/current interface speed
- SMART pass state
- read lookahead / write cache state
- ATA lifetime read/write counters when SMART attributes `241/242` are present

## SES / LED Status

`sg_ses` is installed, but the available `/dev/sg*` nodes on this host appear to
be the disks themselves plus an SD card reader. No actual SES enclosure target
has been found yet, so SES is not the regular UNVR drive-LED path.

The working regular-UNVR disk LED path turned out to be vendor-local instead:

- `python3 -c "from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, True)"`
  visibly lit the leftmost disk LED on the tested chassis
- `python3 -c "from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, False)"`
  cleared it again
- toggling slots `1-4` mapped cleanly to `hdd@0-3` in `/sys/kernel/debug/gpio`
  on the tested appliance

That means the current practical LED stance is:

- regular UNVR: validated per-bay LED control over SSH through
  `ustd.hwmon.sata_led_sm.set_fault(slot, toggle)`
- regular UNVR: live LED state can also be inferred from the last `hdd@N`
  output line in `/sys/kernel/debug/gpio`
- `ubntstorage --verbose disk locate 1` is **not** the working path on the
  tested box; the vendor CLI accepted it, but the appliance logged it as an
  unknown disk action and it did not produce a visible bay LED

## UNVR Pro Consistency Probe

The tested UNVR Pro at `192.168.1.174` looks like the same family from the app's
point of view:

- `uname -a` reports the same `alpine-unvr` kernel family
- `/etc/os-release` reports Debian 11 userspace
- `smartctl --scan-open` detects direct SATA disks cleanly
- `/sys/class/enclosure` is also empty

Observed differences on the tested unit:

- only two HDDs were populated during the probe
- `sg_ses` was not installed on the tested UNVR Pro
- operator-confirmed front face layout is `3` bays on the top row and `4` bays
  on the bottom row, all `3.5"` drives
- vendor front-face artwork confirms the printed bay order is `1-3` on the top
  row and `4-7` on the bottom row, which maps to the app's zero-based
  `[[0, 1, 2], [3, 4, 5, 6]]` layout
- UniFi Protect on the tested unit reported the populated disks as `Bay 1` and
  `Bay 2`
- the observed Linux HCTL values for those two populated bays were `7:0:0:0`
  and `5:0:0:0`, which the local app now uses as first-pass slot hints for the
  validated test unit
- the local app now treats those HCTL values as slot hints only, not as real
  disk device labels or SMART targets, until validated Linux disk correlation
  is available
- repeated SSH probing from the automation host can be flagged by UniFi
  IPS/IDS as outbound SSH scanning, which makes the Pro path look flaky until
  the testing host is exempted or that policy is relaxed
- `/usr/sbin/ubntstorage disk inspect` returns the real bay inventory for the
  Pro too, including `nodisk` rows for empty slots, and now backs the app's
  first-pass slot mapping on the tested unit
- `/usr/share/unifi-protect/app/config/default.json` points at a local
  `http://127.0.0.1/api/hardware` service, but that route did not produce
  usable storage inventory with the tested auth paths, so it is not currently
  part of the adapter
- the real per-bay LED plumbing on the Pro appears to be the kernel-owned
  `sata_sw_leds` / SGPO path rather than SES; the decoded slot-to-GPIO map on
  the tested chassis was:
  - slot 1 -> gpio 71
  - slot 2 -> gpio 69
  - slot 3 -> gpio 67
  - slot 4 -> gpio 70
  - slot 5 -> gpio 68
  - slot 6 -> gpio 64
  - slot 7 -> gpio 66
- direct user-space control via `python3-libgpiod` was not safe to claim on the
  tested Pro because all of those SGPO lines were already busy under the kernel
  `sata led gpio` consumer
- the same vendor-local Python helper used on the regular UNVR is present on
  the Pro too:
  - `python3 -c "from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, True)"`
  - `python3 -c "from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, False)"`
- on the tested Pro, those calls toggled the live GPIO state for vendor slot 1
  from `out lo` to `out hi` and back again on the active `hdd@0` line

That means the current practical stance is:

- regular UNVR: generic Linux over SSH, with a built-in `4`-bay profile
- UNVR Pro: the same generic Linux path, with a first-pass built-in `7`-bay
  profile based on the operator-confirmed `3`-over-`4` face layout
- UNVR Pro: experimental SSH LED control can use the same
  `ustd.hwmon.sata_led_sm.set_fault(slot, toggle)` path, but operator-visible
  bay confirmation is still pending and should be treated as provisional until
  someone validates each bay on the chassis

## Practical Path Forward

The best near-term way to represent the UNVR in this app is:

1. treat it as a generic Linux-family SSH host
2. use password SSH if keys are not available
3. render it with the built-in `ubiquiti-unvr-front-4` profile
4. optionally layer Protect API identity data on top later if we want a more
   vendor-specific adapter

## Current App Behavior

- regular UNVR: validated generic Linux SSH path with mapped `4`-bay front view
  and ATA/SATA SMART enrichment, including cache state and read/write volume
- regular UNVR: validated SSH LED control for the `4` front bays through the
  vendor-local `sata_led_sm.set_fault(slot, toggle)` path, with live state read
  back from `/sys/kernel/debug/gpio`
- UNVR Pro: first-pass `7`-bay `3-over-4` front view with the first two bays
  mapped from the validated test unit and empty bays rendered from vendor
  `nodisk` rows, but still dependent on reliable SSH access for live disk
  correlation and SMART detail
- UNVR Pro: experimental SSH LED support now uses the same vendor-local
  `sata_led_sm.set_fault(slot, toggle)` path as the regular UNVR, with command
  success and GPIO-state changes validated on the tested unit
- both UniFi profiles now render with a UniFi-specific face style rather than a
  Supermicro-style red release latch
