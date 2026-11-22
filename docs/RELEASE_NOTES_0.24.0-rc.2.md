# Release Notes - v0.24.0-rc.2

v0.24.0-rc.2 is a testing release candidate, not v0.24.0 final.

## Since v0.24.0-rc.1

- Storage Fabric adds Topology, a wiring diagram with keyboard navigation,
  path tracing, search, pinning, shelf expansion and Fit. It names incomplete
  evidence and unsupported records instead of silently omitting them.
  Refreshes and selection preserve the drawing's scroll and focus. Topology
  node, bay, group, tier and link accessible names retain all visible wording,
  including clipped labels, while preserving native hover titles.
- CORE, SCALE, Linux, ESXi, BMC and QuantaStor collection now distinguish
  unknown occupancy, unread measurements and conflicting identities from
  empty bays or measured zeroes. Saved SMART details follow their configured
  destination, and QuantaStor SMART paths come from the current own-node poll.
- Backup verification describes what was actually checked. Restore refuses
  unsupported schemas, timestamps retention cannot order and changed history
  members before publication. Private copies and content checks bind the
  installed history to validated bytes; separate defence-in-depth work remains.
- History collection keeps failed scopes retryable. Retention scans, JSON
  state reads, mapping reads and writes, remote backup fetches and hardware
  parsers have explicit limits and refusal messages.
- Setup preserves settings outside the form. Generated SSH keys have their
  intended permissions from creation, configured unsafe key files get warnings,
  browser write-origin checks are shared, and failure messages expose fixed
  diagnostic categories rather than private remote text.
- The image installs hash-locked Python dependencies. Changelog fragments and
  smaller internal modules make release preparation and maintenance easier
  without changing their existing interfaces.

The complete per-change entries and limitations are in `CHANGELOG.md`.

## Since v0.23.0

The v0.24.0 line adds immutable-image update and rollback tooling, encrypted
FULL backup and restore paths, segmented-history integrity and index-build
safeguards, and read-only collection and identity checks. Storage Fabric gains
CORE expander PHY counters and path evidence. Shelf and saved-view identity,
refresh handling, heat-map readability, accessibility and state-chip placement
have been refined through the beta series. Read all intervening upgrade notes
in `CHANGELOG.md` before updating from stable.

## Before you test it

Use the normal immutable image update and preserve your operator overrides.
Earlier saved last-good SMART values do not identify their source appliance,
so they are not displayed after this upgrade. A bay regains its values after
its next successful SMART read. Reload Admin pages left open across the
upgrade, particularly the Backups page. A newly refused state file, history
member or hardware reply needs investigation; do not remove the safety limit
or treat a refusal as an empty or healthy result.

Existing installations that save the implicit `default` system through System
Setup should read the configuration migration and credential-storage guidance
in the Advanced Configuration wiki source first. Older upgrades still require
their documented writer-fence mounts and optional CORE read-only PHY-counter
sudo grant. This release does not authorize appliance privilege changes.

The candidate is intended for fixed tags `v0.24.0-rc.2`, `0.24.0-rc.2` and
`dev`. `latest` stays on v0.23.0 and the stable in-app update channel does not
prompt users to install a prerelease. Public demo rebuilding waits for stable.

## Qualification

This is a full release-candidate qualification. No narrow-beta deferrals
apply. See `docs/RELEASE_WRAP_0.24.0-rc.2.md` for actual results. Source
preparation is not publication or deployment approval.
