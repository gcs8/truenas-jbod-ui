# v0.4.0 Plan

This file is the execution plan for `0.4.0`.

`0.4.0` is the chassis/profile release. The point is to move physical layout
logic out of ad-hoc UI code and into a profile-driven model that can support
known hardware and custom operator layouts.

## Release Goal

Introduce a reusable enclosure profile system that can describe validated
hardware and custom operator layouts without needing a new hardcoded render path
for every chassis.

## Non-Goals

- New storage platform adapters
- Quantastor integration
- Full generic “works with every server” promises
- Fancy topology visualization beyond what the profile system needs

## Why This Comes Next

By the end of `0.3.x`, the main pain should be physical presentation and layout
reusability, not platform inventory collection. `0.4.0` should solve that layer
before another backend is added.

## Design Principles

- Profiles describe physical presentation, not storage logic
- Inventory adapters should emit slots in a neutral model
- Rendering should depend on profile metadata, not platform-specific if/else
- Built-in profiles should be explicit and conservative
- Custom profiles should be possible without code changes

## Status Snapshot

As of the current working branch, the core `0.4.0` profile work is effectively
complete:

- built-in validated profiles exist for the known CORE and SCALE hardware
- inventory snapshot generation resolves profile metadata instead of relying on
  scattered platform-specific UI logic
- the renderer consumes profile eyebrow/summary/title/edge-label/face-style and
  row-group metadata
- profile styling now includes tray latch-edge hints so horizontal and vertical
  tray families can keep their release tabs on the correct side
- custom profile YAML loading works through `paths.profile_file`
- example profiles and authoring docs now live in the repo

What remains before a real `0.4.0` release is release-cut sized rather than
feature-sized:

- optional visual refinement for custom profile styling hints
- version/tag/changelog promotion from `0.4.0-dev` to `0.4.0` completed

## Profile System Scope

### 1. Built-In Profiles

Initial built-ins should cover the currently validated hardware:

- CORE:
  - Supermicro CSE-946 style `60`-bay top-loading shelf
- SCALE:
  - Supermicro `SSG-6048R-E1CR36L` front `24`
  - Supermicro `SSG-6048R-E1CR36L` rear `12`

Each built-in profile should define:

- profile id
- display label
- enclosure orientation
- row/column counts
- slot ordering direction
- bay grouping/dividers
- edge labels
- optional service-area styling hints

### 2. Custom YAML Profiles

Goal:

- let operators define custom chassis layouts without editing code

Minimum config model:

- profile id / name
- slot count
- row / column geometry
- ordering direction
- grouping / divider rules
- front vs rear labels
- optional bay sizing / spacing hints

Nice-to-have:

- custom title / subtitle
- notes / operator guidance
- image/reference metadata

### 3. Renderer Refactor

Goal:

- make the UI render from a profile object instead of current hardcoded layout
  assumptions

Targets:

- move CORE `6 + 6 + 3` grouping into profile metadata
- move SCALE front `4 x 6` and rear `4 x 3` geometry into profile metadata
- stop platform-specific labels from being scattered through template logic
- keep the current visual language, just make it configurable

### 4. Mapping / Import Compatibility

Goal:

- make sure manual mappings survive profile-driven rendering

Targets:

- tie mappings to system + enclosure + slot identity, not fragile visual order
- keep export/import backward-compatible where practical
- support manual/operator-defined profile selection without losing saved mapping

### 5. Validation Strategy

Goal:

- avoid building a flexible system that silently renders the wrong physical
  layout

Targets:

- unit tests for profile parsing and slot-order generation
- fixture-style tests for:
  - CSE-946
  - SSG front `24`
  - SSG rear `12`
- visual smoke checks using screenshot capture

## Proposed Milestones

### Phase A: Data Model

- define profile schema
- add built-in profile registry
- choose how config references profiles

### Phase B: Renderer

- refactor UI rendering to consume profile geometry
- migrate current hardcoded CORE/SCALE layouts to built-in profiles

### Phase C: Custom Profiles

- add YAML loading and validation
- expose profile selection through config
- document custom profile authoring

### Phase D: Polish

- screenshot refresh
- docs refresh
- migration notes from old hardcoded layouts

## Exit Criteria For 0.4.0

Ship `0.4.0` when:

- current validated CORE/SCALE layouts are rendered through the profile system
- at least one custom YAML profile can be loaded without code edits
- mapping/export/import still behaves predictably
- the app is easier to extend for another chassis than it was before

## What This Unlocks For 0.5.0

After `0.4.0`, adding OSNexus Quantastor should mainly be:

- new inventory adapter work
- field-mapping work
- maybe a few known built-in profiles

instead of another round of bespoke rendering logic.
