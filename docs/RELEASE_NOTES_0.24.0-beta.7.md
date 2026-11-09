# Release Notes - v0.24.0-beta.7

v0.24.0-beta.7 is the seventh prerelease of v0.24.0. It adds the changes merged
after v0.24.0-beta.6 and is published for testing only. `CHANGELOG.md` lists
each pull request.

## Channel

- GHCR publishes `v0.24.0-beta.7`, `0.24.0-beta.7` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- Heat-map colours now follow absolute temperature bands taken from each
  drive's SMART warning and critical ratings, or documented HDD and SSD/NVMe
  defaults, instead of stretching from the coolest to the warmest bay in view.
  A 49 C disk beside a 48 C one, or 35 C disks on a cool shelf, no longer show
  as red. Tile text meets WCAG AA contrast on every heat colour, the heat value
  no longer covers the device name, and the enclosure title no longer stacks
  one letter per line when Heat Map is on.
- A page reload right after a rename, or a bay-assignment save, while the
  app's own background inventory refresh is running is now served instead of
  answering 503 "server is busy". A request waits at most 5 seconds behind a
  refresh, and a background refresh that runs past 5 minutes is stopped and its
  late result discarded. The appliance call budget is unchanged.
- Storage Fabric on TrueNAS CORE shows SAS expander PHY error counters
  (invalid DWORD, running disparity, loss of DWORD sync, PHY reset problem)
  when the service account has the new read-only `camcontrol smpcmd` sudo rule.
  Each expander is read at most once every 15 minutes.
- Bays and the Rename button announce what they show, Disk Path facts are
  proper term and value pairs, cut-off path text has its full text on hover
  and keyboard focus, and narrow windows no longer overlap grouped bays or wrap
  labels one letter per line.
- The history service keeps SQLite's temporary sort files on the history
  volume whatever `TMPDIR` the Compose file sets, and its dashboard and
  `/healthz` show a read-only, informational census of segmented-history
  segments that still lack the newer read indexes.

## Before you test it

- Upgrading from v0.24.0-beta.6, Compose files, settings, environment
  variables and data formats are unchanged; set `JBOD_UI_IMAGE` to the new tag
  or digest and update as usual.
- TrueNAS CORE: to see expander PHY error counters on an existing service
  account, re-run the admin SSH access bootstrap or append only the new
  `camcontrol smpcmd` rule shown in the admin sudoers preview, then press
  Refresh once. Until then Storage Fabric says "Expander counters not
  available (permission not granted)"; nothing else changes.
- If you use `scripts/update_immutable_deployment.py`, keep the deployment
  under a root-owned directory such as `/opt` or `/srv`, and name any overlay
  you want kept with `--keep-overlay`; a kept overlay may set only `restart`.
- A custom Compose file no longer needs `TMPDIR` set for the history service;
  `SQLITE_TMPDIR` still overrides the history volume folder.
- Health checks that validate the exact `/healthz` field set of the history
  service must accept the new informational `segment_conversion` field.
- Upgrading from v0.23.0 or older, read the v0.24.0-beta.1 to v0.24.0-beta.6
  upgrade notes in `CHANGELOG.md` first; they all still apply.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.7.md` records the checklist evidence for this
prerelease.
