# v0.20.0-dev SAS Fabric Plan

## Goal

Build a read-only SAS fabric/topology surface for TrueNAS CORE systems that can
explain dual-HBA, dual-path, expander, SES, enclosure, and bay impact state
without replacing the existing physical enclosure view.

The current local sketch in `.local-notes/sas-fabric-sketch/` is the working
shape reference. It proves that Archive CORE can expose enough data for a
top-down topology view using existing app data plus CORE SSH probes.

## Validated Inputs

The Archive CORE sketch has live data for:

- combined 60-bay enclosure `LSI-F SAS3x48Front 0c04 + LSI-R SAS3x48Rear 0c04`
- two `mpr` HBAs/paths with asymmetric state
- `mpr1` active for `44` visible member paths
- `mpr0` split between `16` passive and `28` failed member paths
- four SES nodes seen across slot targets: `/dev/ses4`, `/dev/ses8`,
  `/dev/ses5`, and `/dev/ses9`
- `mprutil show adapters` discovery plus per-unit
  `mprutil -u N show adapter/devices/enclosures/expanders/iocfacts`
- `/var/log/messages` MPR/CAM event slices for timestamped recent controller,
  target, retry, NAK, timeout, and connection-loss evidence. On CORE builds
  where that file is root-only, the timestamped source uses a narrow
  `/usr/bin/tail -n 4000 /var/log/messages` sudo permission; otherwise the
  probe falls back to `dmesg` event order.
- a useful bad-cable sample where slot `0` traces through `mpr1 active`,
  `mpr0 fail`, `/dev/ses4`, and `/dev/ses8`

## First Production Slice

Keep the first tracked slice read-only:

1. Add CORE sudo/admin/docs support for narrow `mprutil` read commands.
2. Add a normalized fabric snapshot model with nodes, links, path states,
   selected-object traces, and raw-source evidence.
3. Build a parser/collector around already available SSH command output:
   `camcontrol devlist -v`, `sesutil map`, `sesutil show`,
   `mprutil show adapters`, and per-unit `mprutil -u N show ...`.
4. Expose a small internal API payload suitable for both the main enclosure
   panel and a future dedicated `SAS Fabric` view.
5. Add focused fixture tests before wiring richer UI behavior.

Current implementation status on `2026-05-19`:

- CORE admin/bootstrap/documentation support for narrow read-only `mprutil`
  commands is in place.
- `app.services.sas_fabric` now parses the first `mprutil` controller,
  expander, enclosure, device, and IOC facts shapes used by the sketch.
- inventory collection auto-runs `mprutil show adapters` for CORE systems when
  older saved SSH command lists do not already include it, then dynamically
  discovers `/dev/mpr*` units from that output and runs the missing per-unit
  `mprutil -u N show adapter/devices/enclosures/expanders/iocfacts` reads.
- inventory collection also auto-runs a filtered `/var/log/messages` MPR/CAM
  event probe with `dmesg` fallback, so Disk Path cards can show timestamped
  recent kernel fault evidence when the service user can read syslog directly
  or can use the narrow `/usr/bin/tail -n 4000 /var/log/messages` sudo entry.
- the MPR/CAM event parser now decodes common SCSI CDB lines, CAM status,
  SCSI sense/ASC/ASCQ reasons, retries, and observed MPR `loginfo` codes into
  operator-facing fault families, likely-layer hints, decoded IO context, and
  per-branch Disk Path fault evidence.
- `/api/sas-fabric` returns a normalized read-only graph with nodes, links,
  traces, controller/path summaries, evidence, warnings, and raw command keys.
- focused regression coverage pins multi-HBA discovery, degraded path traces,
  bay trace link chains, and the non-CORE unavailable boundary.
- local Docker Desktop was rebuilt/recreated from this branch on
  `10.13.37.67`; live `/api/sas-fabric` against Archive CORE returned both
  `mpr0` and `mpr1` per-unit command evidence plus expander/enclosure nodes.

## UX Direction

Support both entry points:

- Main enclosure affordance: a topology button/panel under the current
  enclosure that stays synchronized with selected bays.
- Dedicated `SAS Fabric` view: a larger diagnostic workspace that can show more
  topology context without crowding normal enclosure workflows.

Candidate map modes:

- `Physical Trace`: start from the physical bay layout and trace upward through
  path, controller, SES, expander, and pool/vdev impact.
- `Fabric Lanes`: top-down swim lanes by HBA/path, with crossing links allowed
  when the physical wiring says they should cross.
- `Impact Map`: start from a selected bad path/component and show affected
  bays, vdevs, pools, and enclosure objects.

The detail panel should not be named `Slot Details`. Working name:
`Fabric Inspector`, with selected headings such as `Selected Path`,
`Selected Expander`, `Selected Enclosure`, and `Selected Bay`.

## Friendly Names

Production data should retain raw identifiers and allow operator aliases for:

- HBAs/controllers
- SAS paths
- expanders
- SES/enclosure devices
- backplanes
- external cabling or controller-facing ports when discoverable

Aliases should be optional metadata layered over raw topology IDs, not a
replacement for evidence fields used in debugging.

## Diagnostic Decoder References

The current decoder source inventory lives in
`docs/SAS_DIAGNOSTIC_DECODER_SOURCES.md`.

Use the local Seagate SCSI Command Reference and SPC-3 draft as the generic
SCSI command/sense/LOG SENSE backbone, the `baruch/lsi_decode_loginfo`
MIT-licensed reference for Broadcom/LSI MPI/MPR `loginfo` field breakdowns,
the Ultrastar Data102 service spec as an SES/enclosure element reference, and
the Ultrastar HC310/HC555 SATA manuals only for disk-level ATA/SMART
enrichment. Broader `loginfo` table coverage and a proven CORE-safe SAS PHY
counter source are still open gaps.

## CORE Sudo Requirements

For topology-capable CORE installs, keep command-limited sudo narrow:

```text
/usr/sbin/mprutil show adapter
/usr/sbin/mprutil show adapters
/usr/sbin/mprutil show all
/usr/sbin/mprutil show devices
/usr/sbin/mprutil show enclosures
/usr/sbin/mprutil show expanders
/usr/sbin/mprutil show iocfacts
/usr/sbin/mprutil -u * show adapter
/usr/sbin/mprutil -u * show all
/usr/sbin/mprutil -u * show devices
/usr/sbin/mprutil -u * show enclosures
/usr/sbin/mprutil -u * show expanders
/usr/sbin/mprutil -u * show iocfacts
/usr/bin/tail -n 4000 /var/log/messages
```

Do not use a broad `/usr/sbin/mprutil *` rule unless a future platform proves
that the narrow forms cannot work.

Recent MPR/CAM kernel event evidence prefers `/var/log/messages` because syslog
preserves wall-clock timestamps. Some CORE installs make that file readable to
normal users; Archive CORE keeps it `0600 root:wheel`, so the timestamped path
needs the narrow `/usr/bin/tail -n 4000 /var/log/messages` sudo entry. If no
matching syslog rows are available or that permission is absent, the same probe
falls back to `dmesg -a` event order:

```text
messages=$({ tail -n 4000 /var/log/messages 2>/dev/null || sudo -n /usr/bin/tail -n 4000 /var/log/messages 2>/dev/null || true; } | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' || true); if [ -n "$messages" ]; then printf '%s\n' "$messages" | tail -n 400; else dmesg -a | egrep '(mpr[0-9]+:|\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi
```

## Non-Goals For The First Slice

- no SAS write actions
- no cabling assistant that claims certainty beyond collected evidence
- no forced replacement of the existing enclosure/slot UI
- no dependency on packages installed on TrueNAS CORE
- no dynamic alias editor until the data model and first render shape settle

## Validation

Minimum validation before merging the first implementation slice:

- parser fixtures for multi-HBA CORE output
- regression fixture for one failed path affecting many bays
- API shape test for nodes, links, traces, and evidence
- admin sudo preview test proving `mprutil -u N show ...` normalizes to
  `mprutil -u * show ...`
- browser smoke once the first production view exists
