# Release Notes - v0.24.0-rc.3

v0.24.0-rc.3 is a testing release candidate, not v0.24.0 final.

## Since v0.24.0-rc.2

- Storage Fabric Topology groups bays by their physical enclosure, in inventory order, and shows shared expanders, neighbouring shelves and the paths that connect them. Pin a node or cable to inspect its evidence. Wiring checks distinguish a confirmed measurement from an unread, conflicting or partial one; they no longer infer backplane zones from link evidence.
- Dense Topology rows keep bay and port click targets separate. Links retain their identity when evidence changes. Snapshot export keeps an in-flight estimate cancellable and exposes a retry after a failed estimate.
- TrueNAS SCALE and Linux can use read-only sysfs data for SAS paths and PHY counters where the adapter exposes them. Disk details keep direct and multipath evidence tied to the controller and port that supplied it. Unread disk capacity displays as unread instead of showing SES descriptor text.
- Disk synchronization distinguishes a busy request from unknown status and keeps its result current after recovery. Snapshot export offers a retry while inventory is busy.
- Backup scheduling bounds its configuration snapshot, remote backup fetches constrain workspace file handling, and history import verifies the members it publishes. These changes can refuse work that cannot be budgeted or verified safely.
- Playwright 1.65.1 updates the development browser runner. CI checks add dependency auditing, workflow lint, scheduled published-image scanning, secret-history scanning and Topology mutation tests. QA fixtures no longer reuse captured enclosure identifiers or SAS addresses.
- The QA crawler waits for in-flight snapshot export estimates before leaving export states. Request failures and timeouts still fail the crawl (#1533).

See `CHANGELOG.md` for the full list and upgrade notes.

## Since v0.23.0

The v0.24.0 line adds immutable-image update and rollback tooling, encrypted FULL backup and restore paths, segmented-history integrity and index-build safeguards, and read-only collection and identity checks. Storage Fabric gains CORE expander PHY counters and path evidence. Shelf and saved-view identity, refresh handling, heat-map readability, accessibility and state-chip placement have been refined through the beta series. Read all intervening upgrade notes in `CHANGELOG.md` before updating from stable.

## Before you test it

Use the normal immutable-image update and preserve your operator overrides. Reload any Admin or Storage Fabric tabs left open during the upgrade. If a wiring check reports partial or not read, inspect the evidence it names; an unread measurement is not a zero or a pass. The new SCALE and Linux transport data depends on support exposed by the kernel and adapter. Its absence does not prove a path is absent.

The intended image tags are `v0.24.0-rc.3`, `0.24.0-rc.3` and `dev`. `latest` stays on v0.23.0, and the stable in-app update channel does not prompt users to install a prerelease. Public demo rebuilding waits for stable.

## Qualification

This is a full release-candidate qualification. No narrow-beta deferrals apply. See `docs/RELEASE_WRAP_0.24.0-rc.3.md` for results. Source preparation is not publication or deployment approval.
