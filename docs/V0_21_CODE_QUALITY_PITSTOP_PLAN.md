# v0.21.x Code Quality Pitstop Plan

Status: deferred next-cycle direction after `0.20.1`.

`0.21.x` should be a maintenance and confidence cycle. Treat it as a deliberate
pitstop after the Storage Fabric expansion, not a feature catch-all.

## Goals

- reduce Storage Fabric complexity without changing the operator contract
- improve test speed, fixture clarity, and failure messages
- isolate platform-specific collection/parsing seams
- tighten docs and release automation around the now-large validation matrix
- make future `0.22.x` enrichment safer to implement

## Candidate Work

- split `app/services/sas_fabric.py` into smaller platform graph builders if a
  clean boundary emerges from tests
- add reusable fixture builders for CORE, SCALE/Linux SES, Quantastor, generic
  Linux, ESXi, and BMC Storage Fabric graphs
- turn static JavaScript string-assertion tests into more behavior-oriented
  browser/unit coverage where practical
- improve Playwright helper scripts for dedicated Storage Fabric screens
- codify the release-candidate browser/API matrix so it can be run with fewer
  one-off scripts
- reduce repeated platform-copy conditionals in `sas_fabric_view.js`
- audit parser normalization helpers for duplicate command/key handling
- clean stale planning or release docs that are no longer active

## Non-Goals

- no deeper Linux sysfs/NVMe feature work
- no new Quantastor HA model changes
- no ESXi RAID-management actions
- no BMC write controls beyond existing identify/locator boundaries
- no major visual redesign unless a regression requires it

## Exit Criteria

- current `0.20.1` behavior is preserved by clearer tests
- release validation is easier to run and explain
- platform-specific Storage Fabric code has cleaner ownership
- `0.22.x` enrichment notes are ready to pick up without rediscovering the
  `0.20.1` decisions

## Ryoko / Codex-Style Review Additions (2026-05-21)

A read-only Codex-style deep review of `v0.20.2` independently validated this
pitstop direction. The review did not surface a stronger new hardening item than
the backup import path validation issue already tracked in the separate review
notes, but it did add useful maintainability and agent-readiness priorities.

### Additional Goals

- shrink the biggest "control-plane switchboards" before the next major feature
  push so platform changes have a smaller blast radius
- make safe local validation obvious for humans and AI coding agents
- separate real-looking public demo release data from deterministic test
  fixtures so clean checkouts can get repeatable results
- add a general CI pre-flight for normal PR/push changes, not only release and
  publish workflows

### Additional Candidate Work

- split `InventoryService` into narrower services around source collection,
  platform enrichment, cache coordination, slot-view construction, and
  SMART/detail persistence
- introduce a small platform/fabric builder registry so CORE, Linux SES,
  Quantastor, ESXi, BMC/IPMI, and generic Linux behavior can move out of central
  branch-heavy builders one platform at a time
- split `admin_service/static/admin.js` into feature modules for runtime cards,
  backup/restore, setup form, TLS trust, profile builder, and storage-view
  editing
- add `AGENTS.md` or `CONTRIBUTING.md` with safe commands, live-data cautions,
  sidecar/full-stack validation tiers, and files/workflows agents should not
  touch casually
- add a safe validation wrapper such as `scripts/dev_check.py --safe`, `make
  check`, or `just check` for sidecar-free checks
- add a broad CI workflow that runs safe Python tests, JavaScript syntax checks,
  and minimal browser/public-demo smoke where fixtures allow it
- give public-demo tests a scrubbed deterministic fixture source or mark the
  real-history path as integration/local-data only
  - v0.21 decision: clean CI validates the checked-in `public-demo/index.html`
    artifact, while live-history generation stays a release-maintainer
    local-data path behind `PUBLIC_DEMO_LOCAL_HISTORY=1`; a checked-in scrubbed
    source fixture is deferred unless clean CI needs generation-path coverage.

### Review Evidence / Hotspots

- `app/services/inventory.py` is about 8.9k lines, with `InventoryService` about
  8.6k lines.
- `app/services/sas_fabric.py` is about 3.2k lines.
- `app/static/app.js` is about 9.7k lines.
- `admin_service/static/admin.js` is about 6.4k lines.
- Existing release-wrap evidence is strong, but the only GitHub workflows found
  in this pass were public-demo publishing and GHCR publishing; a normal CI
  gate would make everyday changes safer.

### Operator Framing

The current layout is operationally similar to a few large switchboards that
control many platform-specific paths. That is understandable after the Storage
Fabric expansion, but the next maintenance cycle should reduce blast radius by
moving platform/workflow behavior into smaller, named panels with clearer tests.
