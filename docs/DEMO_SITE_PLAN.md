# Public Demo Site Plan

Date: `2026-05-15`

Status: planning only. This pass does not add a GitHub Pages workflow, change
runtime code, or cut a `0.18.1` release.

## Decision

Track the public demo idea as a `0.19.0-dev` follow-up unless we deliberately
decide that a docs-only patch release is useful for visibility.

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
- generate or hand-curate sanitized demo payloads with fake hostnames, serials,
  pool names, and disk identifiers
- reuse current browser-side rendering where practical
- disable or hide controls that need a live backend
- add Playwright coverage against the static artifact
- publish through a GitHub Pages workflow only after the static artifact is
  deterministic and scrubbed

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
- at least one sample physical enclosure renders from scrubbed data
- slot details, storage-view navigation, and heat-map mode work against sample
  data
- all live-only actions are disabled or absent
- no browser console errors in the static demo smoke test
- a README/wiki link explains the boundary between demo, offline snapshot,
  full backup, and the real Docker deployment

## Open Decisions

- whether the first sample should be a small synthetic lab or a full
  60-bay-style anonymized fixture
- whether the static site should be generated from the FastAPI templates or use
  a dedicated static shell
- whether local fixture import belongs in the first version or should wait
  until the static sample proves useful
- whether to publish from `main` plus GitHub Actions or keep a dedicated
  `gh-pages` branch
