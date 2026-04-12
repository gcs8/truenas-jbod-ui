# v0.2 Roadmap Notes

This file tracks the intended direction for the next iteration without changing
the expectations of the `v0.1.0` release.

## Release Safety

- `v0.1.0` is preserved as a git tag.
- New work should happen on a separate development branch such as `v0.2.x`.
- If post-release fixes are needed for the current public build, they should be
  tagged as a new dot release such as `v0.1.1` instead of rewriting `v0.1.0`.

## Recommended v0.2 Scope

The goal for `v0.2` is to improve operator awareness and portability without
turning the app into a full storage analytics platform.

### 1. Multi-system and multi-enclosure support

Primary value:

- choose between multiple TrueNAS systems
- choose the intended enclosure when a system exposes more than one shelf
- keep the existing page model and avoid forcing operators into a different UI

Suggested UI shape:

- add a `System` dropdown in the header
- add an `Enclosure` dropdown beside it
- hide or disable pickers when there is only one valid choice

Suggested backend shape:

- define a system list in config
- move host-specific connection settings behind a named system identifier
- keep inventory snapshots scoped to `system + enclosure`

### 2. Mapping export/import

Primary value:

- back up slot calibration data
- restore mappings after rebuilds or migrations
- make it easier to compare or share mapping work between systems

Suggested first pass:

- JSON export
- JSON import with validation
- include app version, timestamp, system identifier, and enclosure identifier

### 3. Compact topology awareness

Primary value:

- make it easier to answer "what pool/vdev would this disk affect?"
- highlight sibling disks in the same top-level vdev

Suggested first pass:

- selected-slot sibling list
- small vdev summary panel or modal
- cross-highlighting between a selected bay and its vdev members

Avoid for `v0.2`:

- a large always-visible graph that competes with the enclosure UI

### 4. Optional SMART summary

Primary value:

- faster triage when touching disks physically
- quick check of heat, age, and last test state

Suggested first pass fields:

- temperature
- power-on hours and age in days
- last SMART test result
- logical and physical block size

Suggested later fields if the data source is stable:

- SAS address
- logical unit identifier
- read cache
- write cache
- negotiated link rate

### 5. Optional multipath/member awareness

Primary value:

- quick visibility into how a disk is presented through CORE
- quick awareness of active versus passive or alternate paths

Suggested first pass:

- multipath device name
- member devices
- controller or HBA labels such as `mpr0` and `mpr1`
- path state if it can be derived reliably

## Foundation Work For Later SCALE Support

Do not try to fully support SCALE in `v0.2` unless it becomes urgent, but do
prepare for it:

- isolate TrueNAS data collection behind an adapter interface
- keep the UI data model generic where practical
- avoid scattering CORE-specific assumptions throughout the service layer

Suggested target shape:

- `TrueNASCoreAdapter`
- `TrueNASScaleAdapter`

## Suggested Implementation Order

1. Add branch-safe versioning and roadmap notes
2. Add multi-system config model
3. Add system/enclosure pickers in the UI
4. Add export/import mapping workflow
5. Add sibling-aware topology summaries
6. Add optional SMART summary fields
7. Add optional multipath/member details
8. Refactor backend collection toward adapter-based design

## Things To Keep Out Of Scope

- full historical event tracking
- heavyweight topology visualization frameworks
- deep performance analytics
- automatic remediation or replacement workflows

The app should stay focused on slot identity, LED control, and situational
awareness for physical disk handling.
