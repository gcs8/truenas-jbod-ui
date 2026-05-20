# Release Notes - v0.20.0

Release date: `2026-05-20`

## Summary

`0.20.0` is the first SAS Fabric release.

The release adds a read-only topology and diagnostics workspace for TrueNAS
CORE systems with Broadcom/LSI MPR HBAs. It does not replace the normal
physical enclosure view; it explains what sits behind it: controllers, SAS
paths, expanders, SES/MPR enclosure objects, inferred backplane zones, and
which bays are affected by recent path faults.

The live Archive CORE validation case is the 60-bay bad-cable slice. The UI can
show the healthy `mpr1 active` leg separately from the degraded `mpr0 fail`
leg, decode recent kernel MPR/CAM evidence, and keep raw identifiers available
beside optional operator-friendly aliases.

## Added

- Read-only `/api/sas-fabric` payload with normalized nodes, links, traces,
  controller summaries, path summaries, evidence, warnings, and raw source
  context.
- CORE `mprutil` parser and collector support for adapter, device, enclosure,
  expander, and IOC facts output.
- Automatic CORE seed probing for `mprutil show adapters`, followed by dynamic
  per-HBA `mprutil -u N show ...` discovery.
- Main enclosure `Topology` panel with HBA/path lanes, affected bay
  highlighting, and a `Fabric Inspector`.
- Dedicated `/sas-fabric` view with `Fabric Lanes`, `Impact Map`,
  `Physical Trace`, and `Disk Path` modes.
- Disk Path branch view for host -> HBA -> SAS path -> expander/SES ->
  backplane zone -> disk context.
- Fault focus, path focus, and disk path cards that jump directly into the
  most useful current SAS Fabric evidence.
- Inline friendly-name alias editor for controllers, paths, expanders,
  enclosures, backplanes, and traces, with raw labels retained.
- SAS Fabric aliases in backup/debug bundle path defaults.
- Recent CORE MPR/CAM evidence collection from `/var/log/messages` when
  available, narrow sudo tail fallback when needed, and `dmesg -a` event-order
  fallback.
- HBA PCIe slot enrichment from `pciconf -lv`, `dmidecode -t slot`, and
  `sysctl dev.mpr.N.%location/%parent` evidence.
- Source-scoped SAS diagnostic decoder modules under
  `app/services/sas_diagnostics/`.
- Decoded event tables for kernel evidence, including CDB, CAM status, SCSI
  status, sense ASC/ASCQ, retries, and Broadcom/LSI `loginfo`.
- Source/confidence metadata for decoder rows:
  `standard`, `standard-partial`, `vendor-reference`,
  `vendor-reference-partial`, `observed`, and `unconfirmed`.
- Expanded T10-backed SCSI decoder coverage for common service-action opcodes,
  12-byte service-action commands, third-party copy/attribute commands, and
  peripheral write-fault ASC/ASCQ values.
- Decoder source inventory in `docs/SAS_DIAGNOSTIC_DECODER_SOURCES.md`.
- Focused fixture coverage for the current Archive CORE bad-cable diagnostic
  shape.

## Changed

- CORE setup, bootstrap preview, and one-time permission push now include the
  narrow read-only SAS Fabric commands instead of requiring a broad
  `mprutil *` sudo rule.
- Dedicated SAS Fabric diagnostics now render path branches first and
  path-leg-scoped fault evidence separately, so a healthy branch is not visually
  grouped under the failed branch evidence.
- The decoded event table now states that rows are individual kernel events,
  while grouped counts remain in the top finding chips.
- The decoded event table now supports event order/time display, paging,
  numeric page buttons, text filtering, and event-type filtering.
- Fabric Lanes ordering now sorts expander and SES/MPR enclosure cards by SAS
  level, handles, slots, and natural labels rather than raw payload order.
- Physical Trace related rows now sort bays naturally, so `Bay 00`, `Bay 01`,
  `Bay 02` render in physical order.
- Disk Path bay selection now follows the profile's physical row layout when
  available.

## Fixed

- Physical Trace controller selection no longer collapses the selected
  component card into the trace-step index column.
- Physical Trace breadcrumbs now de-duplicate/rewind instead of recursing into
  repeated trace paths.
- Related traces remain visible but disabled when already present in the
  breadcrumb trail, so visited bays do not appear to disappear.
- Decoded event table open/closed state now survives unrelated dedicated-view
  re-renders such as opening and canceling alias edits.
- Disk Path fault evidence is scoped to the affected path leg, so `mpr1 active`
  no longer appears under `mpr0` fault evidence.
- T10 opcode labels now correctly treat `B5` as `SECURITY PROTOCOL OUT`.

## Validation Snapshot

Current local validation on
`codex/v0.20.0-kickoff-2026-05-16-post-0.19.0`:

- `python -m unittest discover -s tests -p "test_*.py" -v` passed with
  `428` tests.
- `python -m unittest tests.test_sas_fabric tests.test_parsers tests.test_inventory tests.test_admin_service.MainAppBoundaryTests -v`
  passed with `206` tests.
- `python -m py_compile app/services/sas_diagnostics/common.py app/services/sas_diagnostics/scsi.py app/services/sas_diagnostics/lsi_loginfo.py app/services/sas_diagnostics/decoder.py app/services/sas_fabric.py`
  passed.
- `python -m compileall app admin_service history_service scripts tests`
  passed.
- `node --check app/static/app.js` passed.
- `node --check app/static/sas_fabric_view.js` passed.
- `node --check admin_service/static/admin.js` passed.
- `docker compose up -d --build enclosure-ui enclosure-history enclosure-admin`
- `npx playwright test` passed with `27` tests.
- Pre-cut `GET http://127.0.0.1:8080/livez` and
  `GET http://127.0.0.1:8082/livez` returned `0.20.0-dev`; the final release
  build reports `0.20.0`. History `/healthz` returned `status=ok`.
- Forced live `GET /api/sas-fabric?system_id=archive-core&force_refresh=true`
  returned `available=true`, `warnings=0`, `controllers=2`, `traces=63`, and
  `400` controller event-table rows.
- `git diff --check` passed cleanly after the LF policy and dirty text
  normalization pass.

## Known Limits

- This is read-only. It adds no SAS write actions and no cabling assistant that
  claims certainty beyond collected evidence.
- Persistent SAS PHY hardware counters are still unproven on the current CORE
  host; recent kernel events are separate from hardware counter evidence.
- The current Supermicro backplane mapping remains evidence-scoped. Local BPN
  PDFs help with connector context, but they do not prove SES element indexes
  or MPR diagnostic meanings.
- Broadcom/LSI `loginfo` decoding is useful and attributed, but still curated
  rather than a complete MPI decoder.
