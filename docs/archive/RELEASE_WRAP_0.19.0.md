# Release Wrap - v0.19.0

Date: `2026-05-16`

## Scope

`0.19.0` ships the public demo foundation and the broader offline snapshot
export work from the `0.19.0-dev` cycle.

The release keeps the public demo static and browser-only. It does not add a
hosted FastAPI backend, public credentials path, live LED/control path, or
admin maintenance behavior to GitHub Pages.

## What This Release Locks In

- one shared offline snapshot viewer/export primitive for normal exports and
  the public demo artifact
- selected saved/virtual storage views inside normal offline snapshot exports
- optional whole-system live enclosure snapshots inside normal offline snapshot
  exports
- redaction, history re-keying, SMART summaries, and adaptive history
  downsampling across the combined export
- offline navigation between embedded live enclosures and embedded storage
  views
- self-contained storage-view artwork in exported artifacts
- live-derived TN Core / Supermicro CSE-946 public demo data with critical disk
  identifiers scrambled
- no selected bay on public-demo startup and a 7-day preserved history window
- static GitHub Pages publication for `public-demo/`
- publishability checks and Playwright smoke coverage for the checked-in static
  public demo artifact
- faster heat-map timeline scrubbing in large offline/public-demo artifacts

## Validation

Local validation before the release cut:

- `python scripts\check_public_demo_artifact.py public-demo` passed
- `node --check qa\public-demo.spec.js` passed
- `python -m py_compile scripts\check_public_demo_artifact.py` passed
- workflow YAML parsed locally with PyYAML
- `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js`
  passed
- `python -m pytest -q` passed with `392` tests
- `npx playwright test` passed with `27` tests after the local admin sidecar
  was started
- rebuilt local Docker dev stack reports `0.19.0` on the main UI `/livez`
- history and admin sidecar health checks returned `ok`
- focused exporter/public-demo pytest and Playwright coverage passed during the
  development slice
- `git diff --check` passed with only expected CRLF normalization warnings

## What Still Rolls Forward

- Pages deploy and deployed-URL smoke must run after the workflow file reaches
  `main`.
- Admin-sidecar async "export all the things" bulk jobs remain later work and
  should reuse the same exporter path.
- Local import/demo mode remains later work and should stay separate from full
  backup restore.
- After the tag is published, reopen on the next `-dev` branch.
