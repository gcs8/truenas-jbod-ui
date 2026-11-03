# Release Notes - v0.24.0-beta.5

v0.24.0-beta.5 is the fifth prerelease of v0.24.0. It adds the changes merged
after v0.24.0-beta.4 and is published for testing only. `CHANGELOG.md` lists
each pull request.

## Channel

- GHCR publishes `v0.24.0-beta.5`, `0.24.0-beta.5` and `dev`. `latest` stays on
  v0.23.0.
- The in-app update check reads the newest stable release, so it does not offer
  this beta to a stable install. A beta install reports a development build.
- The public demo and its screenshots stay at v0.23.0 until v0.24.0.

## Highlights

- TrueNAS middleware calls now share one limit per appliance, across inventory
  refreshes, SMART grids, slot drawers and LED actions: 4 calls in flight on
  CORE and 8 on SCALE, in total for the whole app, split between the main UI,
  history and Admin services. When the appliance answers that it is busy, or a whole
  inventory read times out, the app backs off with jitter and shows the API
  source as busy instead of unreachable. Heavy read bursts no longer starve the
  appliance's own alert checks.
- A connection whose login was interrupted now waits, within one call timeout,
  for the login to answer before it closes. A CORE 13 appliance no longer keeps
  a stale API session for it.
- Non-TrueNAS systems are labelled by their own sources. The API chip says "API:
  not used" where a platform has no API, SSH shows "partial" when only optional
  reads failed, and the bay source names UniFi storage, StorCLI, the enclosure
  layout or the BMC instead of the TrueNAS API.
- The UniFi UNVR and UNVR Pro fronts are drawn as the real chassis, numbered as
  the chassis prints them. Slot indices, saved mappings and history are
  unchanged.
- New layout-only faces for the UniFi UNVR-G2, UNVR-G2-Pro, Enterprise NVR and
  Enterprise NVR Core. Bay mapping on them is manual until validated on real
  hardware.

## Before you test it

- Upgrading from v0.24.0-beta.4: the shipped Compose files now set
  `APP_SERVICE_ROLE` (`ui`, `history`, `admin`) on each service. Take the new
  `docker-compose.yml`, or add those lines to a custom Compose file; without
  them each service keeps the whole call limit, as in beta.4.
- Upgrading from v0.23.0 or older, read the v0.24.0-beta.1 to v0.24.0-beta.4
  upgrade notes in `CHANGELOG.md` first; they all still apply.
- `max_concurrent_calls` (environment `TRUENAS_MAX_CONCURRENT_CALLS`) is new.
  Leave it blank for the defaults, 4 on CORE and 8 on SCALE, or set 1 to 10.
  The middleware admits 10 calls per connection, so stay below that. It is the
  total for the whole app: 4 is split 2/1/1 and 8 is split 4/2/2 between the
  main UI, history and Admin services.
- API sessions that earlier versions already left behind on a CORE 13
  appliance stay listed until its middleware restarts. This release stops new
  ones; it does not clear old ones.
- The four new UniFi faces are chosen with `default_profile_id`. Their disks
  show as unmapped until you assign each one to a bay.
- The pre-release security review in #737 still has open items. They will be
  closed before v0.24.0.
- To roll back, set `JBOD_UI_IMAGE` to the previous tag or digest and run
  `docker compose pull` and `docker compose up -d`. Durable state under
  `./config`, `./data` and `./history` is not reverted with the image.

## Release process

`docs/RELEASE_WRAP_0.24.0-beta.5.md` records the checklist evidence for this
prerelease.
