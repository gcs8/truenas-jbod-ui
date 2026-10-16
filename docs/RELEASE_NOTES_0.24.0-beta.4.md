# Release Notes - v0.24.0-beta.4

v0.24.0-beta.4 is the fourth prerelease of v0.24.0. It adds the fixes merged
after v0.24.0-beta.3 and is published for testing only. `CHANGELOG.md` lists
each pull request.

## Channel

- GHCR publishes `v0.24.0-beta.4`, `0.24.0-beta.4` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- TrueNAS CORE setup works from a fresh host. Fill in Home directory with a
  folder on a pool and the one-time bootstrap creates the service account
  through the TrueNAS middleware. It sends its script on stdin, and its SMART
  grants still match after TrueNAS saves them.
- CORE read over SSH draws each backplane and AHCI SGPIO port as its own shelf
  instead of one 60-bay CSE-946, reads pool names without the API, and names
  the real source of its bays.
- An expired or not-yet-valid TrueNAS certificate is named as the problem.
  System Setup refuses to pin it and shows the saved bundle's real failure
  after a reload, and the main page says the certificate check failed instead
  of calling the API unreachable.
- Storage views draw a degraded or faulted pool member as Fault. Views added
  from the AOC-SLG4-2H8M2 template show on the main page and get the ESXi RAID
  IDs only on ESXi, and a 2x M.2 carrier (passive) template was added. Admin
  lists each card or rear-bay group once and keeps them out of the Chassis
  layout list.

## Before you test it

- Upgrading from v0.24.0-beta.3 needs no configuration change. Upgrading from
  v0.23.0 or older, read the v0.24.0-beta.1, v0.24.0-beta.2 and v0.24.0-beta.3
  upgrade notes in `CHANGELOG.md` first; they all still apply.
- On a CORE host read over SSH only, a combined enclosure becomes separate
  shelves with new ids. Save bay assignments made on the old combined view
  again.
- CORE hosts set up by an older bootstrap keep SMART grants that TrueNAS no
  longer matches. Re-run SSH access setup to replace them.
- The bootstrap no longer takes a sudo password. Bootstrap as `root`, or as a
  user with password-less sudo.
- Changing a saved system's platform, host, user or TLS settings asks for its
  API key or password again before Save.
- A `.tar.zst.enc` backup made by a v0.24.0 beta cannot be restored by v0.23.0
  or older. Keep a `.7z` backup, or set `BACKUP_FULL_ARCHIVE_FORMAT=7z`, if you
  may roll back.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.4.md` records the checklist evidence for this
prerelease.
