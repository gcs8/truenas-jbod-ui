# Changelog

## Unreleased

Working branch for the next feature set after `v0.3.0`.

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
