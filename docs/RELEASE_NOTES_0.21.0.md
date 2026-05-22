# Release Notes - v0.21.0

Date: `2026-05-22`

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

## Fixed

- Backup import now rejects unsafe archive member names before restore work can
  touch destination paths. Rejected forms include empty names, POSIX absolute
  paths, Windows absolute/drive/UNC-like paths, traversal, dot components,
  duplicate separators, colon-bearing components, and missing manifest-listed
  members.
- Selected directory restore now validates all selected archive members before
  deleting or replacing an existing target directory.

## Runtime Impact

- No new write-capable Storage Fabric or RAID-management action is introduced.
- No intentional operator-facing Storage Fabric wording, route, or payload field
  rename is included in the maintainability seams.
- Richer platform-native Storage Fabric enrichment remains deferred to v0.22.x
  unless a release gate uncovers an operator-correctness blocker.

## Release Discipline

This release remains subject to the full `docs/RELEASE_CHECKLIST.md` gate before
tagging. The initial release wrap records source-only evidence already gathered
and keeps runtime, Docker, restore, perf, snapshot/export,
docs/wiki/public-demo, GHCR, deployment, and post-release rows blocked until
those gates are actually run and recorded.
