# Release Wrap - v0.15.0

Date: `2026-04-27`

## Status

`0.15.0` is the Supermicro BMC / IPMI release on top of the `0.14.2`
runtime-clarity baseline.

This cut locks in:

- Supermicro BMC-first inventory for the validated FatTwin path
- ESXi as enrichment instead of pretending it is the primary hardware source
- operator-supplied ESXi host prep for Broadcom StorCLI bundles
- cleaned-up release/docs/deployment shape around the new compose defaults and
  optional sidecars

## What This Release Locks In

- the app now has a first-class `ipmi` platform path with saved BMC
  credentials, Broadcom storage monitoring, and BMC-backed slot identify
- validated FatTwin front-six numbering is now pinned to the real Supermicro
  order, and the inferred rear-two presentation is at least structurally in
  place for later live confirmation
- ESXi hybrid detail on the FatTwin path now behaves like a real JBOD story:
  BMC slot truth, StorCLI member detail when available, and host SMART counter
  merge for direct JBOD disks
- admin-side ESXi setup is more honest and usable:
  - `Password Only / No Key` is a first-class auth mode
  - Linux bootstrap/sudoers paths stay disabled for ESXi
  - the operator can upload and install a vendor StorCLI package without the
    repo redistributing Broadcom binaries
- the default deployment story is easier to explain:
  - `docker-compose.yml` for published images
  - `docker-compose.dev.yml` for local builds
  - history/admin sidecars treated as normal optional runtime services

## Validation

Local Windows Docker:

- `317` Python tests passed
- Playwright smoke passed with `15` green / `1` skipped
- Python compile and the main/admin/QA JS syntax checks passed
- rebuilt UI/history/admin services all reported `0.15.0` on `/livez`

Linux dev target:

- the release feature set stayed aligned on the earlier Linux wrap pass
- cached read paths and snapshot-export timing remained dramatically healthier
  there than on local Windows Docker Desktop

## What Still Rolls Forward

- live-confirming the inferred FatTwin rear-two BMC numbering once a rear bay
  is populated
- broader sibling-node confirmation on the remaining FatTwin ESXi systems,
  especially `esxi-ft-node-3`
- deciding whether the backend-only `/api/system-locator` path graduates to a
  visible UI affordance later
- the still-slow local Windows Docker Desktop `history_status` and
  `snapshot_export_estimate` path
