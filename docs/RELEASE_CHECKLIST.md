# Release Checklist

Use this checklist before cutting a tagged release.

The goal is to make releases boring, repeatable, and easy to audit later.

## Scope

- confirm the target version number
- confirm the release branch or snapshot branch is the intended source
- confirm no unrelated scratch files are staged

## Code And Runtime

- run the targeted test suite:
  - `.\.venv\Scripts\python -m unittest tests.test_profiles tests.test_inventory tests.test_history_service tests.test_perf tests.test_perf_harness tests.test_snapshot_export tests.test_admin_service tests.test_release_status`
- run the browser smoke suite against the live app:
  - `npx playwright test`
- if the release includes recent Quantastor topology or cache work, sanity-check:
  - switch away from and back to the active Quantastor view
  - confirm mirrors do not briefly flatten into `disk > data`
  - confirm history does not log fake topology churn after middleware restarts or upgrades
- if the branch is carrying perf work or suspected slowdown risk, run the
  read-only harness against a local app build and save the output for
  comparison:
  - `python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate`
  - compare the generated `data/perf/latest.md` and `data/perf/history.csv`
- rebuild the Docker image from the current branch tip:
  - `docker compose up -d --build`
- confirm the app is healthy:
  - `curl http://localhost:8080/livez`
  - `curl http://localhost:8080/healthz`
- validate the optional-sidecar runtime modes:
  - stop `enclosure-admin` and `enclosure-history`, then confirm
    `enclosure-ui` still works as a standalone deployment
  - bring `enclosure-history` back while keeping `enclosure-admin` stopped, and
    confirm the UI still works normally with history-enhanced paths available
  - with the admin sidecar up, confirm the `Runtime Control` cards show
    `Running` plus `Latest` versions and settle back to a clean aligned state
    after startup or sidecar restarts
- sanity-check the validated platform views in the live UI:
  - CORE
  - SCALE
  - GPU Server Linux
  - VMware ESXi
  - UniFi UNVR
  - UniFi UNVR Pro
  - Quantastor
- if the release includes recent ESXi work, sanity-check:
  - the saved ESXi system renders the `AOC-SLG4-2H8M2` board image with two
    matched member slots
  - the ESXi detail pane stays read-only with LED/write actions hidden
  - the admin setup form recommends `root` and keeps the Linux bootstrap /
    sudoers path disabled for the ESXi platform

## Screenshots

- decide first whether the release actually needs a screenshot refresh:
  - if operator-facing layout or workflow visuals changed materially, regenerate
    the tracked screenshot set
  - if the release is mostly runtime, guardrail, or metadata polish, it is okay
    to keep the current screenshot set intentionally and only verify the
    existing image references still match the shipped workflow story
- when a refresh is needed, regenerate tracked screenshots:
  - `python scripts/capture_readme_screenshots.py`
  - `python scripts/capture_history_export_screenshots.py`
- verify output in `docs/images/screenshots/`
- confirm README image references point at the current release filenames
- if the release changes operator-facing workflows beyond the README overview,
  capture and stage manual screenshots in `docs/images/screenshots/` before the
  tag is cut
- for `0.14.0`, capture at least:
  - admin sidecar `Enclosure / Profile Builder` workspace showing:
    - the profile catalog
    - the builder controls
    - the full-width builder preview
    - either `Slot Ordering` or the `Custom Matrix` layout path
  - admin sidecar `Setup + Maintenance` view if the grouped setup/runtime
    workflow is still featured in the README/wiki
  - main UI selector showing `Live Enclosures`, `Saved Chassis Views`, and
    `Virtual Storage Views` if that grouped runtime model is still called out
    in release-facing docs
  - a saved live-backed chassis view that demonstrates the now-matching
    live-profile tray geometry if that parity work is still featured
  - storage-view history open on a populated internal view such as the NVMe
    carrier or `Boot SATADOMs`
  - the separate CORE `Front 24 Bay` live enclosure on `archive-core` if the
    Linux/runtime sanity work is still featured
  - the Quantastor HA SATADOM runtime view on `QSOSN HA` if the current docs
    still call out the HA-node model
  - the ESXi `AOC-SLG4-2H8M2` live carrier view if the current docs call out
    the first-pass read-only ESXi path
  - the admin maintenance panel showing orphan purge and history adoption if
    those maintenance tools remain part of the README/wiki operator story
  - export snapshot dialog with live size estimate visible if that workflow is
    still featured in the README/wiki
- use release-style filenames for those manual captures, for example:
  - `builder-workspace-v0.14.2.png`
  - `admin-setup-v0.14.2.png`
  - `admin-maintenance-v0.14.2.png`
  - `live-vs-storage-views-v0.14.2.png`
  - `storage-view-history-v0.14.2.png`
  - `archive-core-front-24-v0.14.2.png`
  - `quantastor-satadoms-right-v0.14.2.png`
  - `esxi-overview-v0.14.2.png`
  - `snapshot-export-dialog-v0.14.2.png`
- decide whether each new screenshot is:
  - README-facing and should replace or extend repo image references
  - wiki-facing only and should still be staged in-repo before wiki publish
- if the docs mention degraded history behavior, capture one optional
  history-unavailable state before release as reference material

## Release Notes And Docs

- bump `app/__init__.py` to the release version
- add the release section to `CHANGELOG.md`
- refresh any checked-in draft release-notes file if the repo is using one
- refresh the checked-in release notes file for the target tag, for example
  `docs/RELEASE_NOTES_0.14.2.md`
- review `README.md` for stale version or milestone wording
- review `docs/ROADMAP.md` for stale "current direction" text
- review profile/config docs for dead or outdated comments, especially builder
  mode and custom-profile authoring guidance
- review the repo `wiki/` pages for stale setup or release wording

## Config And Examples

- review `.env.example` if any defaults changed
- review `config/config.example.yaml`
- review `config/profiles.example.yaml`
- confirm no dead config keys or misleading comments remain

## Git Hygiene

- inspect `git status`
- inspect the final commit set with `git log --oneline`
- make a final release-prep commit if needed
- preferred repo flow is:
  - do release work on a `codex/` branch first
  - push that branch as a safety checkpoint before the cut
  - when satisfied, switch to `main` and merge locally with a release commit
    such as `Release v0.10.0`
  - tag the merged `main` commit, not the side branch tip
- this repo does not require a PR to cut a release unless we explicitly decide
  to use one for review
- merge the release branch into `main` only when satisfied
- create the annotated release tag after merge

## Publish

- push `main`
- push the release tag
- publish the repo `wiki/` pages if they changed
- create the GitHub release notes from the final changelog section
- publish the GitHub release page so the `Publish GHCR Image` workflow runs
- wait for the `Publish GHCR Image` Actions run to finish successfully
- confirm GHCR has the expected release tags:
  - `ghcr.io/gcs8/truenas-jbod-ui:vX.Y.Z`
  - `ghcr.io/gcs8/truenas-jbod-ui:X.Y.Z`
  - `ghcr.io/gcs8/truenas-jbod-ui:latest`
- if the GitHub plugin is available in Codex, prefer it for GitHub-side actions
  like PRs, issues, or release-page prep

## After Release

- confirm the pushed tag matches the intended commit
- confirm the GitHub README renders the new screenshots correctly
- confirm the wiki publish completed if applicable
- start a new `Unreleased` section in `CHANGELOG.md` for follow-up work
