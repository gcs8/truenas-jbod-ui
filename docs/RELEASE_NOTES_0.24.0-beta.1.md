# Release Notes - v0.24.0-beta.1

v0.24.0-beta.1 is a prerelease of v0.24.0. It contains every change merged
after v0.23.0 and is published for testing only. `CHANGELOG.md` lists each
pull request.

## Channel

- GHCR publishes `v0.24.0-beta.1`, `0.24.0-beta.1` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- An optional backup scheduler and an admin Backups page take config and FULL
  backups on a schedule, copy them to remote targets, and apply per-class
  retention.
- FULL backups default to a faster encrypted `tar.zst` stream format. Existing
  `.7z` backups still restore.
- The main UI picks up edits to its YAML settings files without a restart.
  `/healthz` separates remote trouble from a local fault.
- TrueNAS hosts can use the JSON-RPC 2.0 websocket API.
- Without a public origin, changes are accepted only from an IP address,
  `localhost` or a name in `ADMIN_ALLOWED_HOSTS`.

## Before you test it

- Read the Upgrade notes in `CHANGELOG.md` first. Several affect existing
  deployments: `ADMIN_ALLOWED_HOSTS` for DNS-name access, the new FULL backup
  format, `/healthz` returning 503 for an unwritable folder, and the removed
  `app.host` and `app.port` keys.
- A `.tar.zst.enc` backup made by this beta cannot be restored by v0.23.0 or
  older. Keep a `.7z` backup, or set `BACKUP_FULL_ARCHIVE_FORMAT=7z`, if you may
  roll back.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.1.md` records the checklist evidence for this
prerelease.
