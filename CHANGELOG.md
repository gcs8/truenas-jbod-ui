# Changelog

## Unreleased

Working branch for the next feature set after `v0.1.0`.

### Added

- Multi-system inventory registry groundwork with per-system connection settings
- Header `System` and `Enclosure` pickers wired through inventory, LED, and mapping routes
- System-aware persistent mapping keys so calibration can be scoped per appliance
- Mapping export/import workflow for the active system or enclosure scope
- Selected-slot topology context with clickable sibling awareness for the current vdev
- Selected-slot peer highlighting that dims non-sibling bays and accents the active vdev set on the enclosure map
- Optional `gmultipath list` enrichment for multipath device mode, path state, and member-device awareness
- Optional `camcontrol devlist -v` enrichment for per-member controller labels such as `mpr0` and `mpr1`
- Example multi-system YAML config showing the intended `v0.2` shape
- Stable enclosure geometry so slot carriers keep a consistent physical-looking size when selection state changes

### Planned focus

- Multi-system and multi-enclosure selection
- Mapping export/import workflow
- Compact topology and sibling-awareness improvements
- Optional SMART summary enrichment
- Optional deeper multipath/member awareness, including controller labeling when safe sources exist
- Enclosure profile metadata for chassis-specific slot grouping and bay proportions
- Adapter groundwork for future TrueNAS SCALE support

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
