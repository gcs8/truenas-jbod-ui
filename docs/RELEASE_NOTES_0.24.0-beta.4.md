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

- TrueNAS CORE setup works from a fresh host. The one-time bootstrap sends its
  script on stdin, creates a missing service account through the TrueNAS
  middleware with a validated UID, and stores SMART grants that still match
  after TrueNAS saves them. It refuses BMC/IPMI targets and any platform
  without a command-limited sudo policy instead of writing one.
- CORE read over SSH draws each backplane and AHCI SGPIO port as its own shelf
  instead of one 60-bay CSE-946, restores pool and vdev labels through
  multipath, and names the real source of its bays.
- An expired or not-yet-valid TrueNAS certificate is named as the problem.
  System Setup refuses to pin it, and late certificate inspect answers no
  longer change a different editor.
- Same-model SES shelves stay separate, Storage Fabric keeps the verified
  parents of cascaded expanders, and disk details show the SAS port and SATA
  PHY counters already collected.
- The non-root ownership helper now holds a writer fence. With the new Compose
  file it refuses to run while a service is up, and services started during
  its run wait for it.

## Before you test it

- Upgrading from v0.24.0-beta.3: take the new `docker-compose.yml` with the
  image. Every service now mounts `./writer-fence:/run/writer-fence:ro`, and
  Docker creates the folder on first start. With an older Compose file the
  services still start and each logs that it is not fenced; stop every service
  yourself before running `prepare_nonroot_bind_mounts.py --apply`. A file or
  symlink at `./writer-fence` stops the containers from starting.
- Upgrading from v0.23.0 or older, read the v0.24.0-beta.1, v0.24.0-beta.2 and
  v0.24.0-beta.3 upgrade notes in `CHANGELOG.md` first; they all still apply.
- On a CORE host read over SSH only, a combined enclosure becomes separate
  shelves with new ids. Same-model SES shelves joined as `A+B` become separate
  shelves too. Save bay assignments made on the old combined view again.
- CORE hosts set up by an older bootstrap keep SMART grants that TrueNAS no
  longer matches. Re-run SSH access setup to replace them. The bootstrap no
  longer takes a sudo password: bootstrap as `root`, or as a user with
  password-less sudo.
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
