# Public Demo Site Plan

Date: `2026-05-15`

Status: first static publication path shipped in `v0.19.0`.

Current code adds a deterministic, live-derived TN Core / Supermicro CSE-946
public demo artifact at `public-demo/index.html`, plus tests proving the
artifact is stable, scrubbed, and explorable without a live backend. The repo
now also includes a GitHub Pages workflow that publishes the checked-in
`public-demo/` directory after static artifact checks pass.

Public demo URL:

- https://gcs8.github.io/truenas-jbod-ui/

Public-facing wiki context:

- `wiki/Public-Demo-Site.md`
- `wiki/Demo-and-Offline-Workflows.md`
- `wiki/Architecture-and-Services.md`

## Decision

Track follow-up public-demo work after `v0.19.0` only when it materially
improves the static sample or local demo/import workflow.

GitHub Pages is a good fit for a static, client-side demo because it can host
HTML, CSS, and JavaScript from the repository:

- https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages
- https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site

It is not a fit for the live FastAPI/Docker app itself. Pages cannot run the
Python backend, Docker Compose sidecars, live SSH/API collection, admin
maintenance actions, or any private appliance credentials.

## Target Experience

The public site should let a visitor explore the product shape without owning
the same hardware:

- open a static project demo from GitHub Pages
- choose one or more scrubbed sample systems
- inspect a physical bay layout, slot details, storage views, and heat maps
- scrub through canned history samples when the demo fixture includes them
- see an obvious demo/offline banner instead of live status controls
- avoid all live write paths, LED actions, admin maintenance actions, secrets,
  and real hostnames

The closest existing concept is the exported offline snapshot HTML. The future
demo should reuse that mental model, but make it friendlier for public
evaluation: a stable site with sample data and possibly a local import path for
scrubbed demo fixtures.

## Possible Implementation Shape

### Phase 1 - Static Sample Demo

- add a committed `site/` or generated `public-demo/` artifact source
  - current: `scripts/build_public_demo.py` generates `public-demo/index.html`
- generate a sanitized demo payload from the TN Core source data with fake
  hostnames and scrambled critical disk identifiers
  - current: `app.services.public_demo_fixture` builds a live-derived TN Core
    60-bay top-loader sample with real make/model/capacity texture, saved
    storage views, SMART summaries, and real history samples
  - current: the pool topology follows the validated CORE 60-bay vdev
    membership pattern, including data `raidz2` groups, pool-level `spares`, `mirror-8`
    special members, and matching empty bays
  - current: the saved `4x NVMe Carrier Card` and `Boot SATADOMs` views are
    included by their real configured view names
  - current: the artifact opens with no bay selected and a 7-day real-history
    window
  - current: critical serial, SAS, NAA, and GPTID values are scrambled
    consistently across live slots, SMART payloads, storage views, and history
- reuse current browser-side rendering where practical
  - current: the public artifact is rendered through the same offline snapshot
    exporter used by normal `Export Snapshot`
- disable or hide controls that need a live backend
  - current: snapshot mode hides setup/export actions and disables live refresh
- add Playwright coverage against the static artifact
  - current: `qa/public-demo.spec.js`
- publish through a GitHub Pages workflow only after the static artifact is
  deterministic and scrubbed
  - current: `.github/workflows/publish-public-demo.yml` checks the static
    artifact, runs the public demo Playwright smoke against the checked-in
    file, uploads `public-demo/`, and deploys through GitHub Pages on `main`
    or manual workflow dispatch

### Phase 2 - Demo Mode In The App

- add an explicit demo/offline mode entry path in the main UI
- load sanitized payloads through the same runtime state shape used by normal
  snapshots where possible
- support sample history bundles for heat-map timeline playback
- keep the mode read-only and visually marked as non-live

### Phase 3 - Importable Demo Fixtures

- decide whether the import target is:
  - a self-contained offline snapshot HTML file
  - a separate JSON demo bundle
  - a scrubbed debug bundle subset
- validate the schema before rendering
- keep imports in browser memory for the public site unless the user is running
  a real local Docker stack

## Safety Rules

- never publish real `config/`, `history/`, `data/`, SSH keys, TLS trust
  material, `known_hosts`, appliance hostnames, real serials, or operator
  notes
- make demo fixtures synthetic or thoroughly scrubbed
- keep the Pages site static and public-safe
- use a clear `Demo / Offline` banner
- do not imply GitHub Pages can connect to a user's NAS or run the admin sidecar

GitHub also notes that Pages sites are public web sites and are subject to
usage limits:

- https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits

## Acceptance Criteria For A First Shippable Demo

- a Pages URL loads a static demo without Docker
  - current: workflow publishes `public-demo/` to
    `https://gcs8.github.io/truenas-jbod-ui/` after the branch reaches `main`;
    the repository Pages source is configured for GitHub Actions
- at least one sample physical enclosure renders from scrubbed data
  - current: the live-derived CSE-946-style 60-bay top-loader renders from
    scrubbed fixture data
- slot details, storage-view navigation, and heat-map mode work against sample
  data
  - current: covered by `qa/public-demo.spec.js`
- all live-only actions are disabled or absent
  - current: covered by `qa/public-demo.spec.js`
- no browser console errors in the static demo smoke test
  - current: covered by `qa/public-demo.spec.js`
- a README/wiki link explains the boundary between demo, offline snapshot,
  full backup, and the real Docker deployment
  - current: README/docs/wiki pages link the public demo and explain the
    boundary

## Open Decisions

- whether to add a second public sample later, or keep the TN Core 60-bay
  sample as the main demo
- whether to keep the static site as one generated offline snapshot artifact
  long-term or add a small index shell after Pages publication
- whether local fixture import belongs in the first version or should wait
  until the static sample proves useful
- whether the later local import/demo mode should reuse HTML snapshots directly
  or accept a smaller JSON fixture
