# GPU Server Notes

Host: `gpu-server` (`10.13.37.213`)

Hardware:
- Supermicro `SYS-2029GP-TR`
- Backplane `BPN-SAS3-218GH-N2`
- Two populated NVMe bays on the right side
- Ubuntu host using `mdadm`

## Access Setup

- SSH user: `jbodmap`
- Installed tools: `smartmontools`, `sg3-utils`, `lsscsi`, `mdadm`, `nvme-cli`
- Sudo rules granted for:
  - `smartctl -x -j /dev/sd*`
  - `smartctl -x -j /dev/nvme*n*`
  - `nvme smart-log -o json /dev/nvme*`
  - `nvme id-ctrl -o json /dev/nvme*`
  - `nvme id-ns -o json /dev/nvme*`
  - `lsscsi -g`
  - `lsblk -OJ`
  - `mdadm --detail --scan`
  - `mdadm --detail /dev/md*`
  - `sg_ses -p aes /dev/sg*`
  - `sg_ses -p ec /dev/sg*`
  - optional LED rules for `sg_ses --set/--clear=ident`

## Current Discovery Results

- SSH access works from the app VM with the existing `id_truenas` key.
- `smartctl -x -j` works for NVMe namespaces and returns good SMART data.
- `mdadm --detail --scan` and `mdadm --detail /dev/md5` work.
- `lsblk -OJ` returns rich namespace, partition, mdraid, UUID, and mount data.
- `/usr/sbin/nvme list-subsys -o json` works for the unprivileged `jbodmap`
  user and exposes the two controller PCIe addresses the current profile uses as
  slot hints.
- `sudo -n nvme smart-log -o json`, `id-ctrl -o json`, and `id-ns -o json`
  work for the NVMe controllers and namespaces the app is probing.

## SES / Enclosure Findings

- No `/dev/sg*` devices were present during probing.
- `sg_map -i` reported `Stopping because no sg devices found`.
- `/sys/class/enclosure` was empty.
- `sg_ses -p aes /dev/sg0` failed because no SG device exists.

Conclusion:
- This host is not currently exposing an SES-managed enclosure device to Linux.
- It is not a good candidate for `sg_ses` slot mapping or LED control unless the
  storage/controller stack changes and starts exposing enclosure services.

## NVMe Topology

Linux sees two NVMe controllers, each split into multiple namespaces:

- `nvme0`
  - serial family: `20452B91C7CF`
  - PCIe path from `nvme list-subsys`: `10000:01:00.0`
  - matching hotplug slot in `/sys/bus/pci/slots`: `106`
- `nvme1`
  - serial family: `20452B91C7BF`
  - PCIe path from `nvme list-subsys`: `10000:02:00.0`
  - matching hotplug slot in `/sys/bus/pci/slots`: `107`

Each controller exposes namespaces `1` through `5`.

## mdadm Layout

OS mirror:
- `nvme0n1p2` + `nvme1n1p2` -> `md127` -> `/boot`
- `nvme0n1p3` + `nvme1n1p3` -> `md126` -> `/`

Data stack:
- `nvme0n2` + `nvme1n2` -> `md1` (`raid1`)
- `nvme0n3` + `nvme1n3` -> `md2` (`raid1`)
- `nvme0n4` + `nvme1n4` -> `md3` (`raid1`)
- `nvme0n5` + `nvme1n5` -> `md4` (`raid1`)
- `md1` + `md2` + `md3` + `md4` -> `md5` (`raid0`) mounted at `/mnt/nvme_raid`

## SMART Example

Example probe:
- `smartctl -x -j /dev/nvme0n2`
- `nvme smart-log -o json /dev/nvme0`
- `nvme id-ctrl -o json /dev/nvme0`
- `nvme id-ns -o json /dev/nvme0n2`

Example useful fields observed:
- protocol: `NVMe`
- model: `Micron_9300_MTFDHAL7T6TDP`
- namespace id: `2`
- temperature: `37 C`
- power on hours: `32283`
- percentage used: `6`
- available spare: `100`
- data units written: `4624968600`
- media errors: `0`
- logical block size: `4096`
- firmware revision: `11300DR0`
- NVMe protocol version: `1.2`
- warning temperature threshold: `75 C`
- critical temperature threshold: `80 C`
- namespace EUI64: `eui.00a075102b91c7cf`
- namespace NGUID: `000000000000001000a075012b91c7cf`

## Practical Recommendation

Use this box as:
- a generic Linux/NVMe/mdadm inventory test box
- a custom profile validation box for non-TrueNAS layouts

Do not currently treat it as:
- an SES enclosure mapping box
- an LED control validation box

If we support it in the app later, the cleanest first pass is likely:
- inventory-first Linux adapter
- mdadm topology presentation
- profile-driven rendering of the two populated NVMe bays on the right

## Current App Integration Status

The local `v0.4.0` app now includes this host as:

- system id: `gpu-server`
- label: `GPU Server Linux`
- default profile: `supermicro-sys-2029gp-tr-right-nvme-2`

Current live behavior:

- renders a `2`-slot rear NVMe enclosure view
- maps slot `00` to `nvme0` / PCI address `10000:01:00.0`
- maps slot `01` to `nvme1` / PCI address `10000:02:00.0`
- surfaces on-demand NVMe SMART for the selected slot through SSH
- surfaces NVMe endurance/write context such as wear remaining, bytes written,
  annualized write rate, and estimated remaining write endurance when SMART
  data exposes it
- enriches Linux NVMe slot detail with controller-native firmware, protocol
  version, namespace GUID, and temperature-threshold data through `nvme-cli`
- synthesizes Linux `mdadm` topology into the shared lower context pane
- uses `EUI64` as the persistent slot identifier when available

Current intentional limitations:

- no SES-backed LED control because the host does not expose `/dev/sg*`
- no automatic generic Linux chassis inference beyond the configured profile and
  its `slot_hints`
