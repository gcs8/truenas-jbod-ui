# Release Checklist

Use this checklist before cutting a tagged release.

The goal is to make releases boring, repeatable, and easy to audit later.

## Scope

- confirm the target version number
- confirm the release branch or snapshot branch is the intended source
- confirm no unrelated scratch files are staged

## Code And Runtime

- run the targeted test suite:
  - `.\.venv\Scripts\python -m unittest tests.test_profiles tests.test_inventory`
- rebuild the Docker image from the current branch tip:
  - `docker compose up -d --build`
- confirm the app is healthy:
  - `curl http://localhost:8080/healthz`
- sanity-check the validated platform views in the live UI:
  - CORE
  - SCALE
  - GPU Server Linux
  - UniFi UNVR
  - UniFi UNVR Pro
  - Quantastor

## Screenshots

- regenerate tracked screenshots:
  - `python scripts/capture_readme_screenshots.py`
  - `python scripts/capture_history_export_screenshots.py`
- verify output in `docs/images/screenshots/`
- confirm README image references point at the current release filenames
- if the release changes operator-facing workflows beyond the README overview,
  capture and stage manual screenshots in `docs/images/screenshots/` before the
  tag is cut
- for `0.8.0`, capture at least:
  - history drawer open on a populated slot with temperature plus read/write
    history visible
  - export snapshot dialog with live size estimate visible
  - offline snapshot HTML opened locally with the frozen banner visible
- use release-style filenames for those manual captures, for example:
  - `history-drawer-v0.8.0.png`
  - `snapshot-export-dialog-v0.8.0.png`
  - `offline-snapshot-v0.8.0.png`
- decide whether each new screenshot is:
  - README-facing and should replace or extend repo image references
  - wiki-facing only and should still be staged in-repo before wiki publish
- if the docs mention degraded history behavior, capture one optional
  history-unavailable state before release as reference material

## Release Notes And Docs

- bump `app/__init__.py` to the release version
- add the release section to `CHANGELOG.md`
- refresh any checked-in draft release-notes file if the repo is using one
- review `README.md` for stale version or milestone wording
- review `docs/ROADMAP.md` for stale "current direction" text
- review profile/config docs for dead or outdated comments
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
- merge the release branch into `main` only when satisfied
- create the annotated release tag after merge

## Publish

- push `main`
- push the release tag
- publish the repo `wiki/` pages if they changed
- create the GitHub release notes from the final changelog section

## After Release

- confirm the pushed tag matches the intended commit
- confirm the GitHub README renders the new screenshots correctly
- confirm the wiki publish completed if applicable
- start a new `Unreleased` section in `CHANGELOG.md` for follow-up work
