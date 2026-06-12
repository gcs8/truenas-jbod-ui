# Release Notes - v0.21.0

Date: `2026-06-11`

`v0.21.0` is a maintenance and confidence pitstop after the Storage Fabric
expansion. It improves release safety, clean-checkout validation, backup import
hardening, and Storage Fabric maintainability without trying to make every
platform feature-identical.

The operator contract remains the same: slot identity, identify/locator
boundaries, physical-disk situational awareness, and honest source-labeled
visibility across CORE, SCALE, Quantastor, Linux, ESXi, and BMC/IPMI paths.

## Changed

- Added contributor rails that document safe validation tiers, live-data
  boundaries, public admin-sidecar cautions, and the v0.21 non-goals.
- Added a safe CI preflight workflow for everyday PRs and protected branch
  changes. The workflow runs source-only gates without secrets, live hardware,
  Docker deployments, or admin sidecar startup.
- Split public-demo validation into two explicit paths:
  - clean checkout validation for the checked-in `public-demo/index.html`
    artifact, suitable for CI and normal contributors;
  - release-maintainer local-history generation behind
    `PUBLIC_DEMO_LOCAL_HISTORY=1`, so real-looking demo data remains deliberate
    and auditable.
- Added Storage Fabric internal selector and builder seams around the existing
  graph assembly path. The new build context, named builders, and provider
  registry reduce branch-heavy switchboard risk while keeping the public API and
  operator-facing payload stable.
- Updated the v0.21 pitstop plan with the completed decisions and the remaining
  v0.22 direction.
- Reduced SSH fanout during inventory, SMART, and Quantastor enrichment by
  batching dynamic follow-up commands through reusable short-lived SSH sessions.
- Clarified Quantastor HA SSH targeting so optional SSH enrichment targets real
  node hosts from explicit config, API-published node addresses, or reachable
  node default-gateway discovery rather than the shared API or management VIP.
- Polished the admin-side HA node SSH helper so operators can review and correct
  node-specific targets before relying on optional SSH enrichment.
- Refreshed roadmap/wiki current-version wording for the `0.21.0` maintenance
  release after the docs gate found stale `0.20.1` copy.

## Fixed

- Backup import now rejects unsafe archive member names before restore work can
  touch destination paths. Rejected forms include empty names, POSIX absolute
  paths, Windows absolute/drive/UNC-like paths, traversal, dot components,
  duplicate separators, colon-bearing components, and missing manifest-listed
  members.
- Selected directory restore now validates all selected archive members before
  deleting or replacing an existing target directory.
- SSH startup backoff and command redaction now reduce repeated transient
  connection attempts and keep sensitive inline command arguments out of
  warnings and debug payloads.
- SSH command batches now preserve results collected before a later command or
  session failure, improving both runtime resilience and release evidence.
- Public-demo browser-smoke CI no longer depends on CI video capture and can use
  a hosted browser channel when provided by the runner.
- Rapid system or Storage Fabric page switches no longer leave opportunistic
  SMART prefetch abort/fallback noise in the browser error console. Genuine
  SMART prefetch misses still degrade through the slot SMART summary UI.
- Browser release-gate coverage now selects occupied restored systems for
  heat-map value assertions and tolerates real restored ESXi/remote inventory
  latency instead of relying on fixture-only timing assumptions.

## Runtime Impact

- No new write-capable Storage Fabric or RAID-management action is introduced.
- No intentional operator-facing Storage Fabric wording, route, or payload field
  rename is included in the maintainability seams.
- Optional SSH enrichment should be quieter under transient failures and should
  avoid shared Quantastor API/VIP endpoints when HA node targets are available.
- Richer platform-native Storage Fabric enrichment remains deferred to v0.22.x
  unless a release gate uncovers an operator-correctness blocker.

## Release Discipline

This release remains subject to the full `docs/RELEASE_CHECKLIST.md` gate before
tagging. The initial release wrap records source-only evidence already gathered
and keeps runtime, Docker, restore, perf, snapshot/export,
docs/wiki/public-demo, GHCR, deployment, and post-release rows blocked until
those gates are actually run and recorded.
