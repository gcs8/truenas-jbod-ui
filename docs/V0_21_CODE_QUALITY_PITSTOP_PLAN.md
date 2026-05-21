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
