# v0.20.1 Platform Parity Plan

Status: planning slice started on 2026-05-20
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

## Non-Goals

- Do not rewrite the inventory architecture in one pass.
- Do not make ESXi write or RAID-management actions part of this parity push.
- Do not install packages on TrueNAS or Quantastor appliances from the app.
- Do not turn the CORE SAS Fabric view into a generic feature name if the
  other platforms only expose weaker transport evidence.
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
Linux `zpool`, `lsblk`, `lsscsi -g`, `sg_ses` AES/EC pages, SSH `smartctl`,
and `sg_ses` identify commands where the operator has granted the needed
permissions.

Gaps:

- no generic discovery/remediation flow that turns `lsscsi -g` enclosure nodes
  into exact recommended `sg_ses` sudo rules
- no first-class capability label for "middleware enclosure rows missing, but
  Linux SES mapping is available"
- no SCALE-specific transport details from stable Linux SAS sources such as
  `lsscsi -t`, `/sys/class/sas_*`, or sg device metadata
- SMART test-history/detail still relies on SSH `smartctl` rather than a rich
  SCALE API path
- fixtures should pin the common SCALE case where API enclosure rows are empty
  and Linux SES is the real slot source

Requirements to document/check:

- `lsscsi`
- `sg3_utils` / `sg_ses`
- `smartmontools`
- command-limited sudo for exact `sg_ses -p aes`, `sg_ses -p ec`, optional
  identify commands, and on-demand `smartctl`
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
hosts that expose stable `lsblk -OJ`, mdadm/NVMe context, vendor-specific data
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

- `lsblk -OJ`
- `smartmontools`
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

2. Admin/setup clarity bite
   - Update admin copy and docs so each platform says which sources are
     required, optional, or unsupported.
   - Add SCALE/Linux SES requirement detection from `lsscsi -g` and existing
     `sg_ses` outcomes.

3. Fixture and parser breadth bite
   - Build the parity fixture pack before expanding behavior.
   - Include ESXi multi-controller / alternate vendor CLI samples and SCALE
     multi-SES samples.

4. SCALE/Linux SES parity bite
   - Normalize shared Linux SES parsing, source status, slot options, SMART
     detail, and identify capability handling across SCALE and Linux.

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
