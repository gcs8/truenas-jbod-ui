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
- run hygiene checks before interpreting other diffs:
  - `git diff --check`
  - confirm this command emits no CRLF normalization warnings; `.gitattributes`
    keeps repo text files LF-normalized on Windows and Linux
- if the release includes recent Quantastor topology or cache work, sanity-check:
  - switch away from and back to the active Quantastor view
  - confirm mirrors do not briefly flatten into `disk > data`
  - confirm history does not log fake topology churn after middleware restarts or upgrades
- run the release performance harnesses against the local release-candidate
  stack and save the CSV-backed artifacts for comparison. If a harness is
  intentionally skipped, record the reason in the release wrap:
  - `python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate`
  - `python scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label release-candidate-history`
  - compare the generated `data/perf/latest.md`, `data/perf/history.csv`,
    `data/history-perf/latest.md`, and `data/history-perf/history.csv`
- rebuild the Docker image from the current branch tip:
  - `docker compose -f docker-compose.dev.yml up -d --build`
- confirm the app is healthy:
  - `curl http://localhost:8080/livez`
  - `curl http://localhost:8080/healthz`
- validate every optional-sidecar runtime mode from the same branch tip:
  - **UI only:** stop `enclosure-history` and `enclosure-admin`, keep
    `enclosure-ui` running, then confirm `:8080/livez`, `:8080/healthz`, and
    the browser smoke path still work without either sidecar
  - **UI + history:** run `enclosure-ui` plus `enclosure-history`, keep
    `enclosure-admin` stopped, then confirm `:8080/livez`, `:8081/livez`,
    `:8081/healthz`, `/api/history/status`, and history-enhanced UI paths
  - **UI + admin:** run `enclosure-ui` plus `enclosure-admin`, keep
    `enclosure-history` stopped, then confirm `:8080/livez`, `:8082/livez`,
    `:8082/healthz`, admin setup/maintenance surfaces, and runtime cards for
    the intentionally stopped history sidecar
  - **UI + history + admin:** run all three services, then confirm UI,
    history, and admin health plus `Runtime Control` cards showing aligned
    running versions after startup or sidecar restarts
- run the Linux QA Docker restore release gate before ship/no-ship:
  - export a full backup from the long-running local Windows Docker admin API,
    not by copying host folders. Use the default restore-grade path set:
    `config_file`, `runtime_overrides_file`, `profile_file`, `mapping_file`,
    `sas_fabric_alias_file`, `slot_detail_file`, and `history_db`
  - example export request:
    `POST http://127.0.0.1:8082/api/admin/backup/export?stop_services=false&restart_services=true`
    with JSON body
    `{"encrypt":false,"packaging":"tar.zst","included_paths":["config_file","runtime_overrides_file","profile_file","mapping_file","sas_fabric_alias_file","slot_detail_file","history_db"]}`
  - copy that exported bundle to the Linux release target
  - create a disposable QA Docker stack on the Linux target using the current
    release-candidate source/image, a separate Compose project name, separate
    runtime directories, and a different port range such as
    `APP_PORT=18080`, `HISTORY_PORT=18081`, `ADMIN_PORT=18082`, and
    `HISTORY_BIND_ADDRESS=0.0.0.0`
  - import the backup through the disposable Linux admin API:
    `POST http://127.0.0.1:18082/api/admin/backup/import?stop_services=true&restart_services=true`
    with the exported bundle as `application/octet-stream`
  - confirm the restored Linux QA stack has the expected systems, profiles,
    storage views, runtime overrides, SAS Fabric aliases, slot-detail cache,
    history DB counts, and healthy UI/history/admin `/livez` and `/healthz`
  - if live validation requires local secret material such as SSH keys, TLS
    trust, or known-hosts files, restore it through an encrypted backup or copy
    it only into the isolated QA runtime directory. Do not bind-mount the
    long-running deployment's config directories into the disposable stack
  - run the browser smoke suite against the restored Linux QA stack, including
    admin smoke where available:
    `PLAYWRIGHT_BASE_URL=http://<linux-target>:18080`
    and `PLAYWRIGHT_ADMIN_BASE_URL=http://<linux-target>:18082`
  - after import, wait for any restored history/background collector pass to
    finish before running perf. Check `:18081/healthz` and do not start the
    main or history perf harness while `collection_running=true`
  - run both perf harnesses against the restored Linux QA stack and record
    Linux-specific labels in the local or target CSV trails:
    `release-candidate-linux-qa-restore` and
    `release-candidate-history-linux-qa-restore`
  - avoid running the main and history perf harnesses in parallel against the
    restored QA stack. The history sidecar may kick off forced inventory/SMART
    work after restore, and that can make the main harness time out on forced
    inventory. If this happens, wait for the collector to settle and rerun the
    affected harness; record the collision in the release wrap
  - when summarizing SAS Fabric diagnostics from API JSON, use the current
    `kernel_diagnostics` payload on controller objects, not an older
    `diagnostics` field name, so event-table evidence is not accidentally
    reported as missing
  - when a SAS Fabric SSH probe warning appears, capture the structured
    `raw.command_failures` rows for command, canonical command, exit code,
    stderr/stdout, controller, context, and criticality instead of relying on
    the shortened UI warning text
  - run snapshot export estimate and download against the restored Linux QA UI,
    including at least one packaging change such as Auto to Force ZIP, and
    verify the exported offline artifact opens with `qa/offline-snapshot.spec.js`
    or an equivalent browser smoke
  - run feature-specific release checks that are not covered by the generic
    suites. For `0.20.0`, that includes restored `/api/sas-fabric`, dedicated
    `/sas-fabric`, Disk Path fault evidence, decoded event-table pagination,
    and SAS Fabric alias persistence
  - save Linux QA evidence under a versioned artifact folder and keep the
    disposable QA stack available until the post-publish deployment sniff test
    passes
  - do not keep raw admin import/export responses as evidence unless they are
    scrubbed. Import responses can echo configured systems and secret-bearing
    fields; keep summarized counts/status instead
  - recheck the long-running Windows and Linux stacks were not disturbed by the
    disposable restore work
- sanity-check the validated platform views in the live UI:
  - CORE
  - SCALE
  - GPU Server Linux
  - VMware ESXi
  - UniFi UNVR
  - UniFi UNVR Pro
  - Quantastor
- if the release includes recent ESXi work, sanity-check:
  - the saved FatTwin ESXi system renders the `supermicro-fat-twin-front-6`
    view with the validated `02 05 / 01 04 / 00 03` numbering and a matched
    top-left test disk in slot `02`
  - direct StorCLI `State JBOD` members still render as `ESXi local JBOD`
    rather than a synthetic RAID class, and the topology reads enclosure-first
    (`ESXi local Enc > slot ... > direct disk`)
  - the ESXi detail pane stays read-only for RAID-management actions, but
    BMC-backed identify control is still available on the validated FatTwin
    path
  - the admin setup form recommends `root`, supports `Password Only / No Key`,
    keeps the Linux bootstrap / sudoers path disabled for the ESXi platform,
    and exposes the ESXi `Host Prep / Vendor Tool Upload` panel
  - if the docs still call out the older `AOC-SLG4-2H8M2` path, confirm that
    saved system still renders the board image and its two matched member slots

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
- for the current ESXi / BMC carry-over cycle, capture at least:
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
  - the ESXi FatTwin front-six view if the current docs call out the newer
    BMC-backed read-only ESXi path
  - the admin `Host Prep / Vendor Tool Upload` panel if the current docs or
    wiki tell operators to stage Broadcom StorCLI bundles there
  - the admin maintenance panel showing orphan purge and history adoption if
    those maintenance tools remain part of the README/wiki operator story
  - export snapshot dialog with live size estimate visible if that workflow is
    still featured in the README/wiki
- use release-style filenames for those manual captures, for example:
  - `builder-workspace-v0.15.0.png`
  - `admin-setup-v0.15.0.png`
  - `admin-esxi-host-prep-v0.15.0.png`
  - `admin-maintenance-v0.15.0.png`
  - `live-vs-storage-views-v0.15.0.png`
  - `storage-view-history-v0.15.0.png`
  - `archive-core-front-24-v0.15.0.png`
  - `quantastor-satadoms-right-v0.15.0.png`
  - `esxi-overview-v0.15.0.png`
  - `snapshot-export-dialog-v0.15.0.png`
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
  `docs/RELEASE_NOTES_0.15.0.md`
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
- after the new image is available, update the real long-running deployments
  cleanly and record a final sniff test for each one:
  - local Windows Docker stack
  - Linux Docker stack
  - production deployment
  - confirm the expected tag/digest/version, service health, and the primary UI
    smoke path on each instance
- if the GitHub plugin is available in Codex, prefer it for GitHub-side actions
  like PRs, issues, or release-page prep

## After Release

- confirm the pushed tag matches the intended commit
- confirm the GitHub README renders the new screenshots correctly
- confirm the wiki publish completed if applicable
- after local, Linux, and production deployments are all current and healthy,
  tear down only the temporary Linux QA restore containers, networks, and
  scratch runtime directories
- start a new `Unreleased` section in `CHANGELOG.md` for follow-up work
