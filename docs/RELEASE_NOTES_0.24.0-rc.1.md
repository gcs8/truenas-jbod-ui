# Release Notes - v0.24.0-rc.1

v0.24.0-rc.1 is a testing release candidate, not v0.24.0 final.

## Since v0.23.0

The v0.24.0 line adds immutable-image update and rollback tooling, encrypted
FULL backup and restore paths, segmented-history integrity and index-build
safeguards, and stronger read-only collection and identity checks. Storage
Fabric gains CORE expander PHY counters and clearer path evidence. Shelf
and saved-view identity, refresh handling, heat-map readability, accessibility
and state-chip placement have been refined through the beta series. Read
all intervening upgrade notes in `CHANGELOG.md` before updating from stable.

## Since v0.24.0-beta.8

- UniFi model bay labels retain full line height rather than clipping at the
  top and bottom, including ENVR Core at 390 px (#1160).
- Storage Fabric shows recent cached evidence as refreshing with its age,
  re-reads once, and re-evaluates aged evidence after idle or back/forward
  restoration. Old, fallback or undated evidence stays stale (#1159).
- Single-bay, saved-view and batch SMART summary reads on the main page stop
  waiting after 15 seconds. An unavailable read offers Retry SMART, while a
  refresh timeout keeps the last reading with an explanation (#1163).

## Before you test it

Upgrading from beta.8 needs only the normal image update. No Compose,
configuration, environment, stored-format or appliance-command changes are
introduced by this candidate. Older upgrades still require their documented
steps, including the optional CORE read-only PHY-counter sudo grant and
writer-fence mounts described in earlier beta upgrade notes.

The candidate is intended for fixed tags `v0.24.0-rc.1`, `0.24.0-rc.1` and
`dev`. `latest` stays on v0.23.0 and the stable in-app update channel does not
prompt users to install a prerelease. Public demo rebuilding waits for stable.

## Qualification

This is a full release-candidate qualification. Every long gate deferred
from beta.8 must pass; no narrow-beta deferrals apply. See
`docs/RELEASE_WRAP_0.24.0-rc.1.md` for actual results. Source preparation is
not publication or deployment approval.
