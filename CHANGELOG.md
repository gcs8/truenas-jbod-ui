# Changelog

## Unreleased

Post-`v0.5.0` follow-up work will land here.

## v0.5.0 - 2026-04-14

Quantastor release that adds the first new storage-platform adapter after the
profile system, keeps CORE/SCALE/Linux behavior intact, and makes the shared
`24`-bay Quantastor HA chassis genuinely operator-usable.

### Added

- First-pass Quantastor REST adapter with support for
  `storageSystemEnum`, `physicalDiskEnum`, `storagePoolEnum`,
  `storagePoolDeviceEnum`, and `haGroupEnum`
- Shared-slot Quantastor profile groundwork for the Supermicro
  `SSG-2028R-DE2CR24L`, rendered as a first-pass shared front `24`-bay view
- Quantastor storage-system inventory correlation that can render pool, member,
  and active-owner context through the existing slot-map UI
- Dedicated Quantastor REST client coverage alongside the inventory/profile
  tests that already exercise the first-pass adapter end to end

### Changed

- Bumped the app to `v0.5.0`
- Refreshed config and env examples so Quantastor user/password auth and
  API-first setup are documented alongside CORE, SCALE, and generic Linux
- Quantastor snapshots now surface live HA context from cluster metadata,
  including current master-node warnings and IO-fencing-disabled warnings when
  the appliance reports them
- Quantastor shared-slot mapping now normalizes the validated appliance's mixed
  slot string formats so the real occupied bays render as physical slots
  `0-7` and `12` instead of collapsing the last occupied disk into slot `8`
- Quantastor pool-device slot metadata now outranks the broken per-node
  hardware slot rows when the appliance disagrees with itself, so the validated
  spare disk stays in physical slot `12` in the live UI instead of slipping
  back to slot `8`
- Quantastor HA warnings now prefer the real node records over the synthetic
  cluster aggregate object when evaluating IO fencing state, avoiding false
  "disabled" alerts on validated clusters where both nodes report fencing
  enabled
- Quantastor snapshots can now supplement the REST payload with SSH `qs`
  `disk-list`, `hw-disk-list`, and `hw-enclosure-list` rows, improving shared
  slot truth and slot-detail enrichment on validated clusters
- Quantastor slot details now surface richer operator context for shared-face
  HA systems, including the selected node view, the current cluster master,
  pool-active owner, fence owner, and which nodes currently report visibility
  into the selected slot
- Quantastor slot SMART views now surface the appliance fields that are useful
  without inventing history, including SMART health status, block size, TRIM
  support, SSD life remaining, firmware, transport, SAS address, temperature,
  and predictive / non-medium / uncorrected error counters when the REST or
  SSH `qs` payload provides them
- Quantastor SMART drill-down can now merge host `smartctl` output over SSH
  when sudo is available, surfacing verified power-on time, SSD rotation,
  form factor, and read/write cache state instead of leaving those rows blank
- Quantastor slot detail can now overlay live `sg_ses` AES / enclosure-status
  metadata, including real identify state and enclosure-side SAS slot mapping,
  when one of the HA nodes exposes a working SES path over SSH
- Slot-status matching now ignores nested raw key names like `isFaulty=false`,
  preventing healthy Quantastor spares from rendering as red faulted bays just
  because the raw payload includes a boolean flag name that contains "fault"
- Quantastor can now probe multiple SSH node hosts for a working SES path,
  overlay live `sg_ses` slot metadata onto the shared `24`-bay face, and drive
  identify LEDs through the validated node-local enclosure even when the
  selected storage-system view is the opposite HA node
- Quantastor LED control now prefers the validated SSH `sg_ses` path over the
  appliance REST and `qs` identify methods, because the documented controller
  actions are still being rejected by the active LSI driver path on this
  cluster
- Quantastor slot correlation now lets verified SES presence and SAS-address
  truth override stale appliance slot hints, keeping the validated spare on
  physical slot `12` instead of letting it slip back to slot `8`
- Quantastor SMART and SES probing now prefer the cached working enclosure host
  first, so shared-HA clusters stop fan-out probing both nodes once the app
  already knows which node can talk to the live SES path
- Quantastor CLI enrichment now follows the same cached-host preference and
  host-fallback order as SES and `smartctl`, and Quantastor snapshots now load
  SES discovery first so the later CLI pass can reuse the known working node
  instead of always starting on the default SSH host
- Quantastor slot detail and tooltips now surface the active SES host plus
  SES-side attached-SAS / state-bit fallback data when `smartctl` or the
  appliance payload leaves those values blank
- Top-latch tray rendering now leaves the status LED visible and tones the
  latch down properly on dimmed peer slots, improving the shared-front `24`-bay
  Quantastor view
- Quantastor SMART candidate selection now prefers stable `disk/by-id/scsi-*`
  and direct `/dev/sd*` aliases over unsupported `disk/by-path/*` names, so
  validated spare slots keep their verified power-on, form-factor, rotation,
  and cache data after SES-driven slot remapping
- Enclosure views now warm SMART summaries in the background through a bounded
  batch API path, so tooltip/detail fields are ready faster without repeated
  per-hover round-trips once a view has loaded
- Background SMART warming is now generation-aware and self-healing across
  auto/manual refreshes, so the app keeps previously-known tooltip data visible
  while refreshing it in place and can recover late slots if a browser-side
  prefetch request gets interrupted instead of leaving them stuck loading
- Quantastor now auto-selects the live pool owner when you land on the shared
  HA view without an explicit node selection, and enclosure SMART warming can
  overlap a couple of chunk requests at once so late CORE slots stop trailing
  as far behind the early ones

### Docs

- Added a dedicated `v0.5.0` Quantastor execution plan and updated the
  Quantastor working note to record the current API-first design and current
  limitations
- Refreshed the README and in-repo wiki source so `v0.5.0` setup guidance now
  includes shipped Quantastor support instead of describing it as future work

## v0.4.0 - 2026-04-14

Chassis/profile release that generalizes enclosure rendering, extends Linux
support beyond TrueNAS, and hardens CORE/SCALE parity for richer SMART and
topology detail.

### Added

- First-pass `0.4.0` chassis/profile system work, including built-in validated
  enclosure profiles for the supported CORE and SCALE hardware
- Profile-driven tray latch/release-tab orientation so different chassis
  families can keep horizontal and vertical tray visuals aligned with the real
  hardware
- First-pass generic Linux SSH-only inventory support for profile-driven NVMe /
  `mdadm` hosts, validated against the `gpu-server` SYS-2029GP-TR right-side
  dual-bay NVMe layout
- NVMe SMART endurance/write-volume surfacing for Linux hosts, including wear
  remaining, available spare, bytes written, annualized write rate, estimated
  remaining write endurance, and richer slot hover tooltips
- Optional `nvme-cli` enrichment for Linux NVMe slots, including firmware
  revision, NVMe protocol version, namespace GUIDs, and warning/critical
  temperature thresholds
- SAS/SCSI lifetime read/write surfacing for CORE and SCALE slots when
  `smartctl` exposes processed-byte counters in the error log
- CORE alias matching for dual-path SAS devices so slot inventory can resolve
  pool topology through peer HBA legs, including `special` vdev members that
  only appear under alternate `da*` names in `pool.query`
- Profile-aware snapshot and rendering plumbing so enclosure eyebrow/summary,
  panel labels, edge labels, face styles, row grouping, and slot ordering now
  come from profile metadata instead of scattered hardware-specific UI logic
- Custom profile loading through `paths.profile_file` / `PATH_PROFILE_FILE`

### Planning

- Added detailed `0.3.x` and `0.4.0` execution-plan documents so ongoing work
  can resume from repo files instead of chat context alone.

### Docs

- Added an active roadmap note that captures the intended release order of
  `v0.3.x` parity work, `v0.4.0` chassis profiles, and `v0.5.0` Quantastor
  support
- Added a SCALE note about the appliance REST deprecation alert and recorded
  that this app currently uses the websocket / JSON-RPC middleware path rather
  than `/api/v2.0`
- Added a profile authoring guide and example custom profile file so the
  `0.4.0` chassis/profile system can be adopted without code edits
- Refreshed the README to point at `v0.4.0` CORE/SCALE screenshots and
  added profile migration notes for deployments moving from hardcoded layouts to
  built-in or custom profiles
- Added Ubuntu / `mdadm` / NVMe notes for the `gpu-server` generic Linux test
  host and refreshed config examples to show the SSH-only Linux command set
- Added a Quantastor planning note for the dual-node Supermicro
  `SSG-2028R-DE2CR24L` chassis so the shared-slot representation problem is
  captured ahead of `v0.5.0`
- Added a GitHub-wiki-ready page set under `wiki/` so beginner quick-start
  docs, platform setup guides, profile docs, and troubleshooting can be
  reviewed in-repo before being published to the GitHub wiki

## v0.3.1 - 2026-04-14

Patch release focused on CORE SMART detail restoration and refreshed release
screenshots.

### Fixed

- CORE slot SMART summaries now merge sparse API JSON with `disk.smartctl -x`
  text output before falling back to SSH, restoring fields such as read/write
  cache state, transport, SAS addresses, and negotiated link rate
- SSH SMART text enrichment now carries the same transport and cache fields when
  the app does need to fall back to host-side commands
- README screenshots now wait for populated slot details and use fresh `v0.3.1`
  captures for both the validated CORE and SCALE views

## v0.3.0 - 2026-04-13

Third public release focused on first-pass TrueNAS SCALE support, richer slot
identity, and Linux-side enclosure awareness.

### Added

- First-pass TrueNAS SCALE rough-in with a selectable `offsite-scale` system profile
- Linux SES AES parsing for SCALE through `sg_ses -p aes`
- Linux SES enclosure-status parsing for SCALE through `sg_ses -p ec`
- Split SCALE enclosure views for a front `24`-bay map and rear `12`-bay map
- SCALE disk-to-slot correlation using Linux `lunid` plus parsed SAS addresses from AES pages
- SCALE per-slot SMART summary through SSH `smartctl -x -j` when the websocket API does not expose detailed SMART JSON
- SCALE transport detail in slot summaries through SSH `smartctl -x -j`, including logical unit ID, SAS address, attached SAS address, and negotiated link rate
- SCALE identify LED control through `sg_ses --dev-slot-num=<slot> --set=ident` and `--clear=ident`
- Persistent identifier labeling in slot details so SCALE slots can show `PARTUUID`
  or `WWN` instead of falling back to an empty CORE-style GPTID field
- Fresh release screenshots for both the validated CORE and SCALE views

### Changed

- SCALE slot warnings now explicitly call out Linux SES AES parsing when TrueNAS does not expose enclosure rows
- SCALE slot warnings now reflect Linux SES fallback and live identify-state reads through enclosure-status pages
- SCALE SMART fallback now prefers SSH `smartctl` JSON and only falls back to metadata-only placeholders when SSH SMART reads fail
- SCALE SMART fallback now accepts advisory non-zero `smartctl` exit codes when valid JSON/text output is still available
- SCALE front and rear enclosure views now use front/rear chassis wording instead of inheriting the older top-loader UI labels
- README, sample config, and sample env docs now describe the current SCALE
  `sg_ses` and on-demand `smartctl` workflow instead of the older placeholder
  story

## v0.2.0 - 2026-04-12

Second public release focused on operator awareness, multi-system selection,
and richer CORE shelf context.

### Added

- Multi-system inventory registry groundwork with per-system connection settings
- Header `System` and `Enclosure` pickers wired through inventory, LED, and mapping routes
- System-aware persistent mapping keys so calibration can be scoped per appliance
- Mapping export/import workflow for the active system or enclosure scope
- Selected-slot topology context with clickable sibling awareness for the current vdev
- Selected-slot peer highlighting that dims non-sibling bays and accents the active vdev set on the enclosure map
- SMART phase 1 basics with per-slot temperature, last SMART test result, power-on age, and logical/physical sector size
- Optional `gmultipath list` enrichment for multipath device mode, path state, and member-device awareness
- Optional `camcontrol devlist -v` enrichment for per-member controller labels such as `mpr0` and `mpr1`
- Multipath presentation cards that summarize active, passive, and failed HBA/controller paths when available
- Example multi-system YAML config showing the intended `v0.2` shape
- Stable enclosure geometry so slot carriers keep a consistent physical-looking size when selection state changes

## v0.1.0 - 2026-04-12

Initial public release of the TrueNAS JBOD Enclosure UI.

### Highlights

- 60-bay Supermicro CSE-946 style top-loading enclosure view
- TrueNAS CORE middleware websocket integration for disk, pool, and enclosure data
- Optional SSH enrichment for `glabel`, `zpool`, and SES-backed shelf mapping
- SSH identify LED control through `sesutil locate` when the TrueNAS enclosure API
  does not expose writable rows
- Per-slot detail pane with pool, vdev, topology, serial, gptid, and health data
- Manual calibration workflow with persistent JSON mappings
- Single-container Docker Compose deployment with bind mounts for config, data,
  logs, and SSH material
