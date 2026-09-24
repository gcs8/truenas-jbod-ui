# v0.22.x Storage Fabric Enrichment Notes

Status: parking lot for richer features deferred from `0.20.1`.

Use this file to hold the platform-native Storage Fabric ideas that are useful
but no longer belong in the `0.20.1` release candidate. `0.21.x` should focus
on code quality first.

## Deferred Feature Buckets

### Linux And SCALE

- deeper `/sys/class/sas_*` detail for SAS hosts, ports, phys, expanders, and
  end devices when the platform exposes it
- clearer NVMe subsystem/controller presentation from `nvme list-subsys -o json`
- better read-only source labels for Linux block, SCSI, SES, NVMe, mdadm, and
  SMART evidence
- possible enrichment of path member rows with sysfs topology when it can be
  proven without unsafe host changes

### Quantastor

- HA owner/fence/visibility clarity in the dedicated Storage Fabric view
- SES host/source labeling when Quantastor exposes enough context
- optional endpoint diagnostics for partial REST failures such as `haGroupEnum`
  or `storagePoolDeviceEnum`

### ESXi

- StorCLI/PercCLI breadth beyond the currently validated controller shapes
- clearer controller/member grouping when multiple controllers expose the same
  EID/slot numbers
- richer read-only datastore/LUN/member presentation without implying RAID
  write support

### BMC / IPMI

- source labeling for BMC-only slot/chassis maps
- clearer boundaries between BMC physical evidence and OS/storage evidence
- no write/control expansion unless the operator explicitly asks for it

### Diagnostics

- incremental decoder table growth from specific evidence or source references
- better source attribution for standard, observed, vendor-reference, and
  unconfirmed rows

## Rule For Pulling Work Forward

Only pull an item back into a patch release if it fixes a live regression or
prevents operators from interpreting existing data correctly. Otherwise keep it
for `0.22.x`.
