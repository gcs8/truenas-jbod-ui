# ESXi Platform Feasibility

Status: live probe confirmed; read-only first-pass implementation is wired.
Date: 2026-04-25

## Scope

This note captures the first support shape for adding ESXi as a platform in the
app. It is intentionally not an implementation plan for full VMware management.
The near-term goal is to answer one practical question:

Can the app render an ESXi host's local storage, especially an internal
Supermicro AOC-SLG4-2H8M2 M.2 RAID adapter, with enough physical member detail
to be useful?

## Current Lab Target

- ESXi host: `10.88.88.20`
- Optional IPMI/BMC: `10.88.88.10`
- Optional vCenter: lab vCenter appliance
- Adapter: Supermicro `AOC-SLG4-2H8M2`
- Reported storage shape: two M.2 NVMe SSDs in RAID1 with two exposed LUNs
- Observed host name: `CryoStorage-ESXi.gcs8.io`
- Observed ESXi version: `7.0.3`, build `24411414`

Do not store root credentials in the repo or docs. For live validation, use
temporary interactive access or the existing app SSH-secret flow once an ESXi
platform adapter exists.

## Hardware Notes

Supermicro describes the AOC-SLG4-2H8M2 as a low-profile PCIe Gen4 x8 M.2 RAID
boot adapter with a Broadcom 3808/SAS3808 hardware RAID controller, two M-Key
M.2 sockets, NVMe/SATA M.2 support, RAID0/RAID1 support, MCTP/BMC-enabled
management, and VMware OS support.

The user manual also documents StorCLI as the controller-management path. That
matters more than the generic ESXi device list, because the app needs physical
member truth to show the two M.2 sockets accurately behind the RAID virtual
drives. The manual's slot-numbering note is especially useful:

- NVMe M.2 slot numbering is `0` and `1`.
- SATA M.2 slot numbering is `0` and `4`.

The card has on-board activity/status LEDs, but that is not the same as a
remote per-slot identify LED interface. Treat LED control as unsupported until
the live controller, BMC, or vendor tool proves a safe identify path.

Sources:

- [Supermicro AOC-SLG4-2H8M2 product page](https://www.supermicro.com/en/products/accessories/addon/AOC-SLG4-2H8M2.php)
- [Supermicro AOC-SLG4-2H8M2 user manual](https://www.supermicro.com/manuals/other/AOC-SLG4-2H8M2.pdf)
- [Broadcom ESXCLI storage reference](https://developer.broadcom.com/xapis/esxcli-command-reference/latest/namespace/esxcli_storage.html)
- [Broadcom ESXCLI NVMe reference](https://developer.broadcom.com/xapis/esxcli-command-reference/latest/namespace/esxcli_nvme.html)

## Live Probe Results

The live read-only probe on `2026-04-25` moved this from theoretical to
practical.

vCenter read-only access works and can see the lab inventory:

- datacenters: `GCS8`, `CryoStore`
- connected target host: `cryostorage-esxi.gcs8.io`
- local target datastore: `CryoStore-Local`
- datastore records include capacity, free space, VMFS type, accessibility,
  thin-provisioning support, and whether the datastore is multi-host

That confirms vCenter is useful for labels and datastore context, but not
enough for physical RAID-member truth.

Direct ESXi SSH exposed the important hardware path:

- Broadcom/SAS3808 adapter appears as `vmhba3` using `lsi_mr3`.
- Installed VIBs include `lsi-mr3`, `lsiprovider`, and `vmware-storcli64`.
- ESXi sees two local BROADCOM `SAS 3808` logical SSD devices.
- Both logical devices report `Drive Type: logical`, `RAID Level: RAID1`, and
  `Number of Physical Drives: 2`.
- ESXi also sees a local BROADCOM `VirtualSES` enclosure-services device.
- VMFS mapping is visible:
  - `CryoStore-Local` is backed by the larger VM/data virtual drive.
  - `OSDATA-*` is backed by the smaller ESXi boot virtual drive.
- `esxcli storage core device raid list` maps both logical devices to
  physical locations `enclosure 13 slot 0` and `enclosure 13 slot 1`.
- ESXi SMART against the logical devices is not useful: the logical devices
  report SMART as unsupported or not enabled.

StorCLI is the key source:

- `/opt/lsi/storcli64/storcli64` is present.
- `storcli ... J` JSON output works and should be the preferred parser input.
- Controller status is `Optimal`.
- Firmware, driver, PCI address, SAS address, and virtual/physical drive lists
  are all available.
- The card reports one drive group with two RAID1 virtual drives:
  - `ESXi`, `100.000 GB`, `Optimal`
  - `VMs`, `1.720 TB`, `Optimal`
- Both virtual drives list the same two physical members.
- Physical drives are visible as:
  - `13:0`, online, NVMe SSD, Samsung SSD 970 EVO 2TB, connector `C0 x4`
  - `13:1`, online, NVMe SSD, Samsung SSD 970 EVO 2TB, connector `C1 x4`
- Per-drive detail includes temperature, media-error count, other-error count,
  predictive-failure count, SMART alert flag, firmware revision, size, link
  speed, lane width, connector name, connected port number, and row position.

## Viability Summary

ESXi is viable as a supported platform, and the first implementation is
read-only and host-SSH based.

The current app already has the right conceptual pieces:

- platform-specific inventory collection
- SSH command capture and parsing
- profile-driven internal storage views
- Linux precedent for non-SES NVMe storage with no LED support
- history collection once stable slot records exist

The part ESXi does not share with TrueNAS or Quantastor is the data source. It
should not use the TrueNAS websocket or Quantastor REST paths. It should behave
closer to the Linux adapter: run a bounded read-only SSH command set, parse the
outputs, and synthesize a storage view.

## vCenter Fit

vCenter can be useful, but it should not be the first dependency for this
hardware slice.

Use vCenter later for:

- host, cluster, and datastore labels
- VMFS/datastore context around the LUNs
- a friendlier fleet credential model if ESXi support grows beyond one host
- optional cross-host inventory checks

Do not rely on vCenter first for:

- physical M.2 socket mapping
- RAID virtual-drive to physical-member mapping
- controller firmware and physical-drive state
- any LED or identify behavior

Direct host commands plus the controller vendor CLI are more likely to expose
the card truth we need.

## First Implementation Shape

The first ESXi slice is intentionally narrow:

1. `esxi` is available as a platform enum and admin setup option.
2. ESXi is SSH-only inventory: no TrueNAS API, no sudo bootstrap, no write actions.
3. The default ESXi command bundle captures ESXCLI storage context plus StorCLI JSON.
4. StorCLI physical-drive JSON is the authoritative source for the two M.2 member slots.
5. The app includes a two-slot `AOC-SLG4-2H8M2` storage-view template and provisional card image.
6. The inferred ESXi AOC view binds physical members `13:0` and `13:1` to `M2-1` and `M2-2`.
7. Slot health/detail comes from read-only StorCLI fields such as temperature, firmware, media-error count, predictive-failure count, SMART-alert flag, connector, and link speed.
8. LED/identify and RAID-management actions stay disabled.

Generic ESXCLI logical-device/datastore parsing is present as supporting context, but the useful
physical-slot view currently depends on StorCLI being installed and readable.

Live local smoke on `2026-04-25` saved `cryostorage-esxi` through the admin route
into the ignored local config, restarted the read UI, and confirmed:

- two healthy StorCLI-backed physical members (`13:0`, `13:1`)
- inferred `AOC-SLG4-2H8M2` storage view with `M2-1` and `M2-2` matched
- pool label `ESXi + VMs`
- per-member temperature and media-error counters
- no inventory warnings
- LED/write actions remain disabled

That smoke also widened the StorCLI parser to accept the real ESXi response
shape from this host: virtual-drive rows under `/c0/vN` and physical-drive rows
under `Drive /c0/e13/sN`, not only the more generic `VD LIST` and
`Drive Information` keys.

## Expected Data Levels

Best case, now confirmed on the lab target:

- ESXi sees the Broadcom controller and virtual devices.
- StorCLI is installed.
- We can map two physical M.2 members to one RAID1 virtual drive and two exposed LUNs.
- We can show member health, firmware, temperatures, RAID state, and stable slot positions.

Acceptable first pass:

- ESXi shows two local LUNs/datastores.
- The app renders a read-only ESXi storage summary and clearly labels physical
  member detail as unavailable until the controller CLI is installed or allowed.

Not enough for useful support:

- Only vCenter datastore names are available.
- No host CLI path exposes device IDs, controller IDs, or physical members.
- The card CLI is absent and cannot be installed or queried safely.

## Live Probe Checklist

Run these as read-only commands first. Capture stdout and stderr because ESXi
command availability differs by version and installed VIBs.

```sh
uname -a
vmware -v
esxcli system version get
esxcli software vib list
esxcli storage core adapter list
esxcli storage core device list
esxcli storage core path list
esxcli storage filesystem list
esxcli storage vmfs extent list
esxcli storage san sas list
esxcfg-scsidevs -l
esxcfg-mpath -L
```

For each interesting device or NVMe adapter found above, run the matching
targeted reads:

```sh
esxcli storage core device raid list -d <device>
esxcli storage core device physical get -d <device>
esxcli storage core device smart get -d <device>
esxcli storage core device smart status get -d <device>
esxcli nvme device controller list -A <adapter>
esxcli nvme device log smart get -A <adapter>
```

The lab host did not support `esxcli storage core nvme device list`, and raw
NVMe commands are not the primary path when the disks sit behind the SAS3808
RAID abstraction. Prefer StorCLI for member health.

Then locate any vendor controller tools:

```sh
which storcli
which storcli64
which perccli
which perccli64
find / -name 'storcli*' -o -name 'perccli*' 2>/dev/null
```

If StorCLI is present, run:

```sh
storcli /call show all
storcli /c0 show all
storcli /c0/vall show all
storcli /c0/eall/sall show all
storcli /c0 show all J
storcli /c0/vall show all J
storcli /c0/eall/sall show all J
```

If the binary lives under an ESXi path such as `/opt/lsi/storcli/storcli64`,
use the full path.

## IPMI/BMC Role

IPMI is optional for the first pass. It may help with chassis health, event
logs, and general sensor context, but it probably will not provide the exact
per-M.2 RAID member mapping. Only add IPMI to ESXi support if the host probe
shows a real gap that BMC data can fill.

## Risks And Guardrails

- ESXi root SSH is practical for validation, but production docs should call it
  experimental unless we find a cleaner least-privilege account model.
- ESXi does not use Linux sudo. Do not reuse the Linux bootstrap/sudo flow.
- SMART/NVMe telemetry may be limited by the controller abstraction. Prefer
  controller health fields over pretending raw NVMe SMART is always available.
- Do not add write actions, RAID-management actions, or LED actions in the
  first slice.
- Keep the hardware template separate from the ESXi adapter so the same
  AOC-SLG4-2H8M2 view can be reused for a Linux or future non-ESXi host.

## Recommendation

Keep ESXi support measured and read-only for now.

The first useful deliverable is now implemented: an ESXi inventory adapter that
parses ESXCLI plus StorCLI JSON and exposes:

- controller identity
- virtual-drive to physical-drive membership
- physical M.2 serials and health
- stable slot numbering
- datastore/LUN names for operator context

Those are present on the lab target, so the next useful step is live
operator-facing validation in the app UI, not broad vCenter integration.
