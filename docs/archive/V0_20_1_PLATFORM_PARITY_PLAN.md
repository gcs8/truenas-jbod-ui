# v0.20.1 Platform Parity Plan

Status: planning slice started on 2026-05-20; first Storage Fabric
implementation bite completed on 2026-05-20
Branch: `codex/v0.20.1-kickoff-2026-05-20-post-0.20.0`

This note defines what "functional parity" should mean across TrueNAS CORE,
TrueNAS SCALE, OSNexus Quantastor, generic Linux, and ESXi without pretending
that every platform can expose the same hardware features. The goal is a
consistent operator contract: the app should make each platform's capabilities,
gaps, degraded data sources, and safe actions obvious.

## Evidence Reviewed

- current handoff/TODO state after the `v0.20.0` release and `0.20.1-dev`
  reopen
- `app/services/inventory.py`, especially source-bundle collection,
  platform-specific correlation, SMART/detail, SAS Fabric, and identify paths
- `app/services/system_setup.py` default per-platform command bundles
- `app/services/parsers.py`, `app/services/ssh_probe.py`,
  `app/services/quantastor_api.py`, and storage-view helpers
- `config/config.example.yaml` platform examples
- `docs/V0_3_SCALE_NOTES.md`
- `docs/QUANTASTOR_NOTES.md`
- `docs/V0_5_QUANTASTOR_PLAN.md`
- `docs/ESXI_PLATFORM_FEASIBILITY.md`
- `docs/SSH_READ_ONLY_SETUP.md`
- `docs/STORAGE_VIEW_PLAN.md`

## Working Definition Of Parity

Parity is not identical features. It is a predictable operator experience:

- a system can be added with clear platform-specific setup requirements
- inventory renders a useful physical or logical storage view from supported
  sources
- slot details, SMART detail, history, and topology labels are stable where
  the platform exposes enough identity
- unsupported actions are visibly unavailable for a reason, not silently broken
- warnings say which source failed and which user-facing feature is affected
- debug/export evidence preserves the raw source context needed to reproduce
  or support a problem

## Storage Fabric Direction

Operator direction changed after the first SCALE/Linux SES pass: the dedicated
fabric surface should become **Storage Fabric**, not a CORE-only SAS Fabric
page. CORE remains the gold-standard example for the kind of facts worth
bubbling up, but every platform should render the best honest path graph it can
from its own evidence. That means:

- CORE continues to expose deep HBA, path, expander, SES, bay, pool/vdev, disk,
  and decoded fault evidence.
- SCALE/Linux SES can expose host/source, SG enclosure, SES slot, bay, disk,
  pool/vdev, SMART, and identify evidence; future Linux SAS sysfs reads may add
  controller/phy/domain context when present.
- Quantastor can expose storage-system/HA-node, hardware-enclosure/SES host,
  disk, pool, spare, owner/fence, SMART, and optional `qs`/`sg_ses` evidence.
- ESXi can expose host, controller, virtual drive, physical member,
  datastore/LUN, SMART, and StorCLI/PercCLI health evidence.
- Generic Linux can expose host, block-device, NVMe, mdadm, filesystem,
  storage-view/profile, SMART, and optional SES/BMC/vendor evidence.

Research notes backing the next bites:

- Linux kernel `libsas` documentation says the SAS sysfs tree shows the current
  physical SAS domain layout and device parameters, which makes it a candidate
  enrichment source where hosts expose `/sys/class/sas_*`.
- `sg_ses` can join Element Descriptor, Enclosure Status, Additional Element
  Status, and threshold pages, and can address elements by descriptor, device
  slot number, or SAS address where the enclosure supports those fields.
- `lsblk` reads sysfs and udev and has JSON output, making it a good low-risk
  generic Linux/NVMe/SATA relationship source.
- `smartctl -j` provides machine-readable SMART detail; `nvme smart-log`
  exposes NVMe SMART log pages where `smartctl` is weak or unavailable.
- ESXi `esxcli storage` exposes device SMART, RAID member, path stats, device
  lists, and UID maps; vendor CLI output such as StorCLI can expose controller,
  virtual-drive, physical-drive, drive-group, and boot-drive topology.
- Quantastor `qs` CLI documents hardware disk list/search, hardware enclosure,
  disk identify, pool add/read-cache/spare/log operations, and disk-list
  filters spanning storage systems, pools, spares, vendor/product, and disk
  type.

## Non-Goals

- Do not rewrite the inventory architecture in one pass.
- Do not make ESXi write or RAID-management actions part of this parity push.
- Do not install packages on TrueNAS or Quantastor appliances from the app.
- Do not claim unproven physical hops. Weak evidence is still useful when it is
  labeled as logical, inferred, or platform-native.
- Do not hardcode lab-only system ids, controller numbers, SAS addresses, or
  chassis assumptions.

## Capability Matrix

| Capability | CORE | SCALE | Quantastor | Linux | ESXi | Parity target |
| --- | --- | --- | --- | --- | --- | --- |
| Add/setup flow | Mature CORE API plus SSH bootstrap guidance | First-pass API plus Linux SSH/SES guidance | REST plus optional HA-node SSH notes | SSH-only setup with profiles | SSH-only plus operator-supplied host-prep for vendor CLI | Admin should show per-platform required, optional, and unsupported sources before save. |
| Inventory source | TrueNAS middleware plus FreeBSD SSH | TrueNAS middleware plus Linux SSH/SES | Quantastor REST plus optional CLI/SES SSH | SSH command bundle and optional BMC/vendor data | ESXCLI plus StorCLI/PercCLI-style vendor CLI | Source status should be feature-scoped: inventory, slots, SMART, identify, diagnostics. |
| Physical slots | Strong SES/live profile path | Works when `sg_ses` AES/EC is enabled | Works on validated shared-face SES host | Profile/vendor/SES dependent | Profile plus StorCLI physical members | Show physical slots where proven; otherwise fall back to storage views with honest capability labels. |
| Storage topology | ZFS pool/vdev/spares/multipath | ZFS pool/vdev/spares via API/SSH | Quantastor pools, HA ownership context | mdadm/NVMe/vendor labels where known | Datastore, LUN, virtual-drive, member context | Normalize labels enough that detail/history views read consistently, while preserving platform-native context. |
| SMART/detail | API plus SSH enrichment | SSH `smartctl` primary for detail | REST fields plus SSH `smartctl` enrichment | SSH `smartctl`/`nvme` enrichment | ESXCLI SMART plus StorCLI health/member data | Prefer the richest safe source, cache/persist detail, and show why a source was skipped or missing. |
| History | Stable live slots and storage views | Stable where slot identity is mapped | Stable, but HA-node dimension needs care | Stable for profile/manual/storage views | Stable for profile/mapped local members | History scope ids must stay stable across refresh and include HA/node context where needed. |
| Identify/LED | CORE `sesutil locate` when allowed | Linux `sg_ses` identify when allowed | SES identify through best working HA host | Only where SES/BMC/vendor support is proven | Disabled | UI should show identify capability per slot/enclosure, not just per platform. |
| Platform details | CORE physical/SAS context | Linux transport details can grow | HA node, owner, fence/visibility context | Vendor/BMC/NVMe context | ESXi host/controller/datastore context | Provide a quiet platform-details panel for context, separate from slot facts. |
| SAS/transport diagnostics | Deep CORE SAS Fabric and decoder | Not equivalent today | Not equivalent today | Not equivalent today | Not equivalent today | Keep CORE SAS Fabric as-is; add weaker platform diagnostics only under capability-specific names. |
| Debug/export | Supported | Supported | Supported | Supported | Supported | Debug bundles should include capability/source status plus scrubbed raw command/API evidence. |

## Current Platform State And Gaps

### TrueNAS CORE

CORE is the reference implementation for full physical JBOD operation: TrueNAS
middleware inventory, FreeBSD SSH enrichment, SES slot mapping, SMART detail,
identify LEDs, ZFS topology, history, and the new SAS Fabric diagnostic view.

Remaining parity work:

- keep CORE as the "deep capability" baseline while avoiding CORE-only
  assumptions in shared UI copy
- keep SAS Fabric explicitly tied to discovered CORE `mprutil` evidence
- make source warnings feature-scoped so a transient diagnostic probe failure
  does not look like an inventory or topology failure

New requirements: none for parity beyond the current narrow SSH permissions.
The `v0.20.0` release already captured the expanded CORE `mprutil`,
`dmidecode`, `pciconf`, and `/var/log/messages` tail requirements.

### TrueNAS SCALE

SCALE support is real but should be described as Linux-SES backed, not "CORE
with a different API." The current adapter can use middleware disk/pool data,
Linux `zpool`, stable-column `lsblk --json`, `lsscsi -g` /
`lsscsi -g -t`, `sg_ses` AES/EC/join pages, SSH `smartctl`, and `sg_ses`
identify commands where the operator has granted the needed permissions.

Gaps:

- no generic discovery/remediation flow that turns `lsscsi -g -t` enclosure
  nodes into exact recommended `sg_ses` sudo rules
- no first-class capability label for "middleware enclosure rows missing, but
  Linux SES mapping is available"
- transport detail is still limited to what `lsscsi -g -t` and `sg_ses --join
  --filter` prove; deeper `/sys/class/sas_*` correlation remains a future
  enrichment path
- SMART test-history/detail still relies on SSH `smartctl` rather than a rich
  SCALE API path
- fixtures should pin the common SCALE case where API enclosure rows are empty
  and Linux SES is the real slot source

Requirements to document/check:

- stable-column `lsblk --json`
- `lsscsi -g` and `lsscsi -g -t`
- `sg3_utils` / `sg_ses`
- `smartmontools`
- command-limited sudo for exact `sg_ses -p aes`, `sg_ses -p ec`,
  `sg_ses --join --filter`, optional identify commands, and on-demand
  `smartctl`
- optional `nvme-cli` if a SCALE system has internal NVMe media that should be
  represented through storage views

### Quantastor

Quantastor support is REST-first with optional SSH/CLI/SES enrichment. The app
already models each storage system as a selectable enclosure-like view, uses
REST disk/pool rows, can merge `qs` CLI evidence, can use `sg_ses` for a real
shared-face 24-slot chassis, and can enrich SMART detail with SSH `smartctl`.

Gaps:

- HA shared-face behavior needs a clearer long-term model for active owner,
  visible node, fence owner, and failover history
- SES host discovery is still manual through SSH host and `extra_hosts`; there
  is no generic "find the node with the working SES controller" flow
- optional REST endpoint failures such as `haGroupEnum` and
  `storagePoolDeviceEnum` need better debug classification
- history scope design may need a node/owner dimension if disks move across HA
  ownership or visibility states
- docs/admin copy should distinguish global spare/pool-spare/grouping behavior
  from vdev grouping, like the ZFS spare grouping fix just did for CORE data

Requirements to document/check:

- Quantastor API endpoint, user, and password
- optional SSH to one or more HA nodes
- optional `qs` CLI access
- `sg_ses` and `smartctl` sudoers on the node that can see the SES device
- `ssh.extra_hosts` or an equivalent admin UX for redundant HA-node SSH paths

### Generic Linux

Linux support is intentionally source/profile dependent. It works well for
hosts that expose stable-column `lsblk --json`, mdadm/NVMe context, vendor-specific data
such as UniFi storage commands, or a configured profile/storage-view binding.

Gaps:

- generic physical slot binding is not solvable without SES, BMC, vendor data,
  manual mappings, or a profile with slot hints
- source ordering should prefer richer SSH detail for SMART/storage-view slots
  while avoiding slow live refreshes
- eMMC/MMC boot media wear remains limited where firmware does not expose good
  counters
- Linux SES support should converge with SCALE where possible instead of
  becoming a parallel parser path
- BMC enrichment should remain optional and capability-scoped so BMC-only facts
  do not overwrite better host-side identity

Requirements to document/check:

- stable-column `lsblk --json`
- `smartmontools`
- optional `lsscsi -g -t` for SCSI/SG transport detail
- optional `nvme-cli`
- optional `mdadm`
- optional `sg3_utils` / `sg_ses` for SES-backed chassis
- optional vendor command set, such as UniFi storage commands
- optional BMC/IPMI credentials for locator or slot metadata where proven

### ESXi

ESXi support is deliberately read-only. The current adapter uses SSH, ESXCLI,
and vendor storage CLI output, with the validated path centered on StorCLI JSON
for controller, virtual-drive, physical-member, and health mapping.

Gaps:

- controller discovery is too centered on `/c0`; support should discover
  `/call` or enumerate controllers before issuing per-controller reads
- Broadcom StorCLI path variants and Dell PercCLI equivalents need a small
  detection matrix
- remaining FatTwin ESXi nodes still need breadth checks so the validated node
  is not accidentally special-cased
- the host-prep upload/install flow needs operator feedback before growing
  delete/history/verification features
- a dedicated least-privilege ESXi credential story remains unproven; root SSH
  is accepted for the current dev/lab cycle but should stay visibly called out
- vCenter labels/fleet metadata can remain later/optional unless live operator
  feedback shows the host-local view lacks enough context

Requirements to document/check:

- ESXi SSH enabled for the saved host
- ESXCLI storage commands available
- a vendor storage CLI installed, currently validated with
  `/opt/lsi/storcli64/storcli64`
- optional operator-supplied `.zip` or `.vib` package for host-prep
- no Linux sudo/bootstrap flow; ESXi should not inherit SCALE/Linux setup copy

## Cross-Cutting Work Packages

1. Platform capability contract
   - Add or formalize capability flags for inventory, physical slots, SMART
     detail, history, identify, platform details, and diagnostics.
   - Expose the same flags to main UI, admin setup, and debug bundle summaries.

2. Feature-scoped source status
   - Keep existing API/SSH/BMC status, but add feature-level impact so warnings
     can say "SMART detail partial" or "identify unavailable" instead of
     implying the whole platform failed.
   - Reuse the calmer SAS Fabric warning approach for non-CORE platforms.

3. Platform fixture pack
   - Add scrubbed or synthetic source bundles for SCALE no-enclosure-API plus
     Linux SES, Quantastor REST plus optional endpoint failures, generic Linux
     NVMe/mdadm, and ESXi StorCLI variants.
   - Tests should cover multi-controller ESXi and multi-HBA/multi-SES Linux
     paths without assuming `c0`, `mpr0`, `mpr1`, or one SG enclosure.

4. Admin setup requirements
   - Make each platform's add/edit flow show required, optional, and unsupported
     sources.
   - For SCALE/Linux SES, add a discovery-guided requirement checklist that
     turns observed SG devices into exact operator commands.
   - For ESXi, keep host-prep separate from normal setup and say clearly when a
     vendor CLI is installed but no controller is visible.

5. Storage views as the common denominator
   - Treat storage views as the parity surface for internal media, virtual
     devices, boot devices, and non-enclosure hardware.
   - Keep detail/history behavior identical once a storage-view slot has stable
     identity, regardless of platform.

6. Platform details panel discipline
   - Use a quiet context panel for platform-native facts: Quantastor HA owner,
     ESXi datastore/controller, Linux/BMC source details, SCALE Linux transport.
   - Do not move unstable context into primary slot identity.

7. Validation matrix
   - Add focused parser/unit tests for every new parser branch.
   - Use targeted browser smoke only when UI capabilities or warnings change.
   - Reserve heavy release gates and production sniffs for behavior changes
     with real runtime risk.

## Recommended Implementation Order

1. Capability/status design bite
   - Document and expose the platform capability flags without changing data
     collection behavior.
   - Add tests for current CORE, SCALE, Quantastor, Linux, and ESXi capability
     summaries.
   - First pass completed on 2026-05-20: inventory snapshots now include a
     typed `capabilities` payload for inventory, physical slots, SMART detail,
     history, identify LEDs, platform details, and diagnostics. Tests pin the
     current CORE, SCALE, Quantastor, generic Linux, and ESXi contract without
     changing collection behavior.

2. Admin/setup clarity bite
   - Update admin copy and docs so each platform says which sources are
     required, optional, or unsupported.
   - Add SCALE/Linux SES requirement detection from `lsscsi -g -t` and existing
     `sg_ses` outcomes.
   - First pass completed on 2026-05-20: admin state now exposes a structured
     setup `requirements` contract beside recommended SSH commands, and the
     setup UI renders Required, Optional, and Unsupported guidance for every
     platform. SCALE/Linux SES guidance calls out stable `lsblk --json`,
     `lsscsi -g -t`, exact `sg_ses -p aes/ec` plus
     `sg_ses --join --filter` rules for discovered SG devices, and
     unsupported CORE/BSD tools; ESXi guidance stays read-only and notes
     `/cN` or `/call` StorCLI edits for non-`c0` controllers.

3. Fixture and parser breadth bite
   - Build the parity fixture pack before expanding behavior.
   - Include ESXi multi-controller / alternate vendor CLI samples and SCALE
     multi-SES samples.
   - First pass completed on 2026-05-20: fixture coverage now includes SCALE
     empty middleware enclosure rows plus Linux SES, Quantastor optional
     endpoint failures, Linux NVMe/mdadm, and ESXi non-`c0` plus same-slot
     multi-controller StorCLI samples.

4. SCALE/Linux SES parity bite
   - Normalize shared Linux SES parsing, source status, slot options, SMART
     detail, and identify capability handling across SCALE and Linux.
   - First SAS Fabric-adjacent pass completed on 2026-05-20: SCALE/Linux
     snapshots with SG enclosure slot evidence now build a dedicated
     `linux_ses` graph for `/api/sas-fabric`, and the dedicated view labels it
     as Linux SES mapping instead of CORE SAS Fabric. This is intentionally
     weaker than CORE: it shows host/source, SG enclosure path, bay, pool/vdev,
     and disk relationships, while HBA and expander hop detail remain absent
     unless a future Linux source proves them safely. Generic Linux selections
     without SG enclosure slot evidence remain unavailable instead of entering
     the Linux SES view.
   - Follow-up unsupported-state polish completed on 2026-05-20: Quantastor,
     ESXi, and IPMI keep the CORE SAS Fabric boundary, but API payloads now
     report `fabric_kind=platform_unsupported` and the dedicated view labels
     the selected platform instead of rendering CORE-specific page chrome.
   - Storage Fabric reshape completed on 2026-05-20: the dedicated fabric
     surface now presents as `Storage Fabric`, and `/api/sas-fabric` returns
     `raw.fabric_domain=storage_fabric` with best-effort platform graph kinds
     for Linux SES, generic Linux/NVMe/storage-view evidence, Quantastor HA/SES
     and pool evidence, ESXi StorCLI/controller/member evidence, and BMC/IPMI
     slot evidence. This supersedes the earlier non-CORE unavailable boundary
     for platforms that have useful storage evidence, while still avoiding
     unproven HBA/expander hop claims.
   - SCALE/Linux enrichment follow-up completed on 2026-05-20: saved and live
     SSH payloads now normalize stable-column `lsblk --json`,
     `lsscsi -g` / `lsscsi -g -t`, and `sg_ses --join --filter` evidence.
     SCALE/Linux Storage Fabric bay traces can now carry SG device, SCSI HCTL,
     transport protocol/address, attached SAS address, phy id, Linux block
     record, Linux SCSI row, and selected-disk SMART summary in the inspector.
   - NVMe subsystem follow-up completed on 2026-05-20: SCALE/Linux refreshes
     now add a guarded `nvme list-subsys -o json` probe when it is missing from
     the saved SSH command list, classify direct NVMe probe failures as
     optional Storage Fabric enrichment, and include that source in setup and
     capability guidance so existing installs can pick up controller/PCIe-path
     context without requiring a config rewrite.

5. Quantastor HA clarity bite
   - Improve HA node/SES host selection evidence, optional endpoint warnings,
     and history-scope decisions.

6. ESXi breadth bite
   - Generalize controller enumeration and vendor CLI path detection, then
     validate against the remaining FatTwin hosts before widening docs.

7. Optional UI polish bite
   - Add subtle capability indicators only where they reduce operator confusion.
   - Avoid a dashboard of badges unless user testing shows the status is hard
     to understand.

## Open Questions

- Should platform parity be reported per saved system, per selected enclosure,
  or per slot?
- Should Quantastor history scope include active-owner or presenting-node once
  HA/failover evidence is richer?
- Should the ESXi root/password dev decision stay documented as acceptable for
  lab installs, or should the next pass prioritize a dedicated account/key
  model?
- Should non-CORE transport diagnostics get their own page later, or should
  they remain platform-details/debug-bundle evidence until a real failure case
  proves a dedicated surface is useful?
- Which live systems are safe to sniff for read-only parity validation without
  disturbing production?
