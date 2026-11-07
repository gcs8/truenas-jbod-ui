# Release Notes - v0.24.0-beta.6

v0.24.0-beta.6 is the sixth prerelease of v0.24.0. It adds the changes merged
after v0.24.0-beta.5 and is published for testing only. `CHANGELOG.md` lists
each pull request.

## Channel

- GHCR publishes `v0.24.0-beta.6`, `0.24.0-beta.6` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- A single-row chassis face that is wider than its bezel, such as a 24-bay
  2.5" front at 1600 px, now scrolls from its first bay. The first bays were
  clipped past the start edge, where scrolling could not reach them and a
  click could not select them.
- Storage Fabric keeps the CORE HBA PHY table in controller data. Each
  HBA-to-expander or expander cascade link can carry `reported_lanes`: the PHY
  pairs both ends report toward each other, with lane count and negotiated
  rate. It stays null when either end's rows are missing or disagree, and it
  does not claim a cable count or width. No new appliance command is run for
  it.
- On a segmented history deployment, the admin page's removed-system history
  lists and the delete-history row count show the real saved row counts
  instead of 0.
- The image is built on a digest-pinned `python:3.12-slim` base, with
  pydantic 2.14.3, websockets 17.4 and boto3 1.43.126.

## Before you test it

- Upgrading from v0.24.0-beta.5, no operator action is expected. Compose
  files, settings, environment variables and data formats are unchanged; set
  `JBOD_UI_IMAGE` to the new tag or digest and update as usual.
- Upgrading from v0.23.0 or older, read the v0.24.0-beta.1 to v0.24.0-beta.5
  upgrade notes in `CHANGELOG.md` first; they all still apply.
- API consumers that check the exact field set of Storage Fabric links must
  accept the new optional `reported_lanes` field.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.6.md` records the checklist evidence for this
prerelease.
