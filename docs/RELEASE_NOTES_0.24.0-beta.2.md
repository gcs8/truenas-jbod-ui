# Release Notes - v0.24.0-beta.2

v0.24.0-beta.2 is the second prerelease of v0.24.0. It adds the fixes merged
after v0.24.0-beta.1 and is published for testing only. `CHANGELOG.md` lists
each pull request.

## Channel

- GHCR publishes `v0.24.0-beta.2`, `0.24.0-beta.2` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- Upgrades no longer roll back healthy systems. The upgrade helper's
  `--inventory-url` disk check counts a disk as placed when any enclosure or
  storage view of the system shows it, and a history database with more than
  250,000 rows builds its new indexes after startup instead of failing the
  first healthcheck.
- Opening a system without picking an enclosure lands on its default again,
  and a cleared bay mapping's note no longer comes back.
- 2.5" bays draw as 2.5" sleds again, a bay's state icon no longer covers its
  latch, LED or labels, and every Connections node and bay can be reached.

## Before you test it

- Upgrading from v0.24.0-beta.1 needs no configuration change. Upgrading from
  v0.23.0 or older, read the v0.24.0-beta.1 upgrade notes in `CHANGELOG.md`
  first; they all still apply.
- A `.tar.zst.enc` backup made by a v0.24.0 beta cannot be restored by v0.23.0
  or older. Keep a `.7z` backup, or set `BACKUP_FULL_ARCHIVE_FORMAT=7z`, if you
  may roll back.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.2.md` records the checklist evidence for this
prerelease.
