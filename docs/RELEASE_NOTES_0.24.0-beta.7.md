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
  no longer covers the device name or breaks inside the number on narrow
  bays such as 24-bay 2.5-inch sleds, and the enclosure title no longer stacks
  one letter per line when Heat Map is on.
- A page reload right after a rename, a Refresh, or a bay-assignment save
  while the app's own background inventory refresh is running is now served
  instead of answering 503 "server is busy". Page loads and other reads wait
  at most 5 seconds behind a refresh; Refresh and saves join the running
  refresh, up to its 5-minute deadline, and start no second appliance read. A
  background refresh that runs past 5 minutes is stopped and its late result
  discarded. The appliance call budget is unchanged.
- The export dialog's size estimate and the export itself no longer fail with
  "Live estimate unavailable" while the app's own background refresh runs:
  after the 5-second wait they use the cached view and say when it was read.
  A page, storage-view, Storage Fabric or SMART read that finds no cached view
  yet is tried again up to three times after the server's Retry-After, with
  the wait shown, instead of ending at "The inventory is still refreshing".
- A bay-assignment save or clear made against a disk that has since left the
  bay is refused with "The disk in this bay changed" instead of storing the old
  disk's serial and notes on the bay, and the page reloads the bay to show the
  new disk. Restoring bay assignments from a backup file names bays that now
  hold a different disk in its preview, and refuses a confirmed preview with
  "Bays changed since the preview" if a bay it writes got another disk since.
- Storage Fabric on TrueNAS CORE shows SAS expander PHY error counters
  (invalid DWORD, running disparity, loss of DWORD sync, PHY reset problem)
  when the service account has the new read-only `camcontrol smpcmd` sudo rule.
  Each expander is read at most once every 15 minutes.
- Bays and the Rename button announce what they show, Disk Path facts are
  proper term and value pairs, cut-off path text has its full text on hover
  and keyboard focus, and narrow windows no longer overlap grouped bays or wrap
  labels one letter per line. The admin Setup + Maintenance page no longer
  scrolls sideways in a narrow window.
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
- Scripts that save or clear bay assignments, or restore them from a backup
  file, through the API should expect a 409 `mapping_disk_changed` or
  `mapping_import_bays_changed` answer when the bay's disk changed; nothing is
  written, and the bay or preview should be read again.
- An export or export estimate made while the app's own background refresh
  runs past 5 seconds now succeeds from the cached view with a warning naming
  when it was read, where it used to answer 503 "still refreshing".
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
