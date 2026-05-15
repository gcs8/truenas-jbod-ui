# v0.17.0 Release-Candidate QA Plan

This is the slow, comprehensive validation pass to run before pushing
`0.17.0` to GitHub / GHCR.

The goal is to prove the current branch against every local system shape we
can safely reach, collect enough evidence to debug failures later, and avoid
turning a release cut into a memory test.

## Safety Rules

- Do not delete, import, restore, purge, adopt, or rewrite real live system
  state during the RC pass unless a step explicitly says to use a disposable
  temporary target.
- All write/change/destructive/failure-mode testing belongs only in disposable
  QA Docker stacks, not in the long-running dev stacks or real production-like
  runtime state. That includes import/restore, config edits, runtime override
  saves, demo label/profile/storage-view edits, orphan purge/adopt/delete
  flows, container stop/start failure-mode checks, and backup round-trip
  mutation tests.
- Run those disposable QA-stack checks on both Windows and Linux when practical.
  If the Linux disposable stack cannot be created, record that as a release
  risk or explicit test-environment gap before cutting the release.
- The only live write action allowed against real systems is drive identify LED
  on/off, and each LED test must end with identify off.
- Do not stage secrets, local `config/config.yaml`, `config/profiles.yaml`,
  `data/known_hosts`, generated `artifacts/`, or copied debug bundles.
- When a test asks for a system-specific sample slot, choose a safe known disk
  with the operator present or already-approved local test hardware.
- Save raw outputs under `artifacts/v0.17.0-rc/<environment>/` unless a script
  already writes to `data/perf/` or a documented QA output path.

## Environments

Run the matrix in both places when practical:

- `local-windows-docker`: Docker Desktop on this workstation.
- `codex-dev-test-linux`: Linux Docker target used for release/perf sanity.
- `local-windows-qa-restore`: disposable local containers on alternate ports,
  restored from the long-running dev stack backup and torn down after evidence
  capture.
- `codex-dev-test-linux-qa-restore`: disposable Linux containers on alternate
  ports, restored from the Linux or exported dev-stack backup and torn down
  after evidence capture.

Before starting, record the active commit and branch:

```powershell
git status --short --branch
git log --oneline -8
```

For the Linux target, copy or pull the same branch tip, then record the same
branch/commit information there before running the stack.

## Static Validation

Run from the repo root on Windows first:

```powershell
.\.venv\Scripts\python.exe -m compileall app admin_service history_service tests scripts
.\.venv\Scripts\python.exe -m unittest tests.test_profiles tests.test_inventory tests.test_history_service tests.test_perf tests.test_perf_harness tests.test_snapshot_export tests.test_admin_service tests.test_release_status -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --check app\static\app.js
node --check admin_service\static\admin.js
node --check qa\ui-switching.spec.js
npx playwright test
git diff --check
```

Repeat the Python/unit and Playwright suites on the Linux target if the target
has the current test dependencies available. If Playwright browsers are missing
there, record that as a test-environment gap instead of silently skipping it.

## Runtime Stack Bring-Up

Build and start the full local stack:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile history --profile admin up -d --build
curl.exe -fsS http://127.0.0.1:8080/livez
curl.exe -fsS http://127.0.0.1:8080/healthz
curl.exe -fsS http://127.0.0.1:8081/livez
curl.exe -fsS http://127.0.0.1:8081/healthz
curl.exe -fsS http://127.0.0.1:8082/livez
curl.exe -fsS http://127.0.0.1:8082/api/admin/state
```

Repeat on the Linux target with the same profiles and save equivalent `curl`
outputs under that environment's artifact folder.

## Metrics And Observability Smoke

For each full-stack environment with metrics enabled, save the scrape payloads:

```powershell
curl.exe -fsS http://127.0.0.1:8080/metrics > artifacts/v0.17.0-rc/local-windows-docker/metrics-ui.txt
curl.exe -fsS http://127.0.0.1:8081/metrics > artifacts/v0.17.0-rc/local-windows-docker/metrics-history.txt
curl.exe -fsS http://127.0.0.1:8082/metrics > artifacts/v0.17.0-rc/local-windows-docker/metrics-admin.txt
```

Confirm the payloads include the expected shared metrics:

- `truenas_jbod_ui_build_info` for UI, history, and admin
- `truenas_jbod_ui_service_up` for each service
- `truenas_jbod_ui_http_requests_total` after the smoke requests above
- UI inventory/cache metrics after at least one main UI inventory load:
  `truenas_jbod_ui_inventory_snapshot_requests_total`,
  `truenas_jbod_ui_inventory_source_bundle_requests_total`, and
  `truenas_jbod_ui_smart_summary_requests_total` when a slot detail was opened
- history collector metrics after a history dashboard/overview pass:
  `truenas_jbod_ui_history_collector_running`,
  `truenas_jbod_ui_history_last_scope_count`, and
  `truenas_jbod_ui_history_last_success_timestamp_seconds`

If the shared Prometheus / Grafana sandbox is available, confirm both checked-in
dashboards load against the RC scrape target without panel query errors:

- `grafana/dashboards/truenas-jbod-ui-backend-overview.json`
- `grafana/dashboards/truenas-jbod-ui-history-data.json`

Record the dashboard state in the environment notes. If the sandbox is not
available, record that as a non-blocking test-environment gap, but do not skip
the raw `/metrics` scrape checks.

## Disposable QA Restore Stack

Run one full portability drill against a fresh, separate container set before
the release cut on Windows and repeat the same disposable-stack shape on the
Linux target when practical. These stacks must not mount the long-running dev
stack's `./config`, `./data`, `./history`, or `./logs` directories directly.

Use separate ports and paths, for example:

- UI: `http://127.0.0.1:18080`
- history: `http://127.0.0.1:18081`
- admin: `http://127.0.0.1:18082`
- working state: `artifacts/v0.17.0-rc/local-windows-qa-restore/runtime/`
- evidence: `artifacts/v0.17.0-rc/local-windows-qa-restore/evidence/`

For Linux, use the same pattern under a separate path such as
`/docker-local/truenas-jbod-ui-qa-restore` and record the chosen alternate ports
and runtime directories in
`artifacts/v0.17.0-rc/codex-dev-test-linux-qa-restore/notes.md`.

Portability flow:

- Export a full backup from the long-running dev admin sidecar.
- Confirm the backup selection includes config, runtime overrides, profiles,
  mappings, slot detail cache, and `history_db`. If the UI grows an explicit
  extra checkbox for DB portability, it must be selected for this drill.
- Start the disposable QA stack from clean empty runtime directories on the
  alternate ports.
- Import the dev backup into the disposable QA admin sidecar.
- Restart the disposable UI/history services if the import flow requires it.
- Confirm the restored QA stack loads the same saved systems, profiles,
  storage views, mappings, runtime overrides, and history counts.
- Confirm the history sidecar on `:18081` shows carried-over tracked scopes and
  metric/event counts, not an empty new database.
- Run a main UI smoke on `:18080`, an admin smoke on `:18082`, and a history
  smoke on `:18081`.

Mutable QA-only checks:

- Edit admin-owned runtime behavior values in the disposable stack and save.
- Restart the disposable read UI and confirm the main UI timing chips reflect
  the changed values.
- Edit a safe QA-only field, such as a demo system label, custom profile label,
  or storage-view display label, then confirm the read UI honors it.
- Export another backup from the disposable stack and confirm the edited values
  and history DB are present in that backup too.
- Re-import that disposable backup into the disposable stack if time allows, to
  prove the edited state round-trips.

Destructive/failure-mode checks:

- Exercise import, restore, purge/adopt/delete previews or executions, runtime
  override edits, intentionally missing sidecar modes, and container stop/start
  failure-mode behavior only in these disposable QA stacks.
- Do not perform those checks against the long-running Windows dev stack, the
  Linux dev stack, or any real production-like state.

Teardown:

- Save logs, screenshots, `docker ps`, `/livez`, `/healthz`, admin state, and
  history overview into the QA evidence folder.
- Stop and remove only the disposable QA containers and their temporary runtime
  directories.
- Recheck the long-running dev stack on `:8080`, `:8081`, and `:8082` after
  teardown to prove it was not disturbed.

## Main UI Browser Sweep

For each saved system visible in the system selector:

- Load the main UI and confirm the version card says `0.17.0-dev` during RC.
- Confirm the refresh timing strip is visible in the toolbar, and the status
  strip shows separate cache timing chips for snapshot, sources, SMART, and
  SES paths.
- Change `Refresh Every` through `15 sec`, `30 sec`, `1 min`, and `5 min`;
  confirm the countdown restarts and the progress bar shrinks/grows normally.
- Disable and re-enable `Auto-refresh`; confirm the timing strip changes state.
- Click `Refresh Now`; confirm it forces a live inventory refresh without
  switching systems or losing the selected enclosure/view unexpectedly.
- Switch every live enclosure, saved chassis view, and virtual storage view
  exposed for that system.
- Click an empty slot, occupied slot, and any mapped/manual slot available.
- Confirm the detail drawer shows persistent IDs, topology, health, SMART, and
  copy buttons without throwing browser console errors.
- Exercise search by serial, device name, pool/vdev, and persistent ID.
- Open history for a populated slot; test every timeframe option and both I/O
  chart modes.
- If the system exposes `Platform Details`, expand/collapse each section.
- If the system has a safe LED target, turn identify on, confirm the UI state,
  turn identify off, then force refresh and verify it stayed off.

System families to cover when available:

- TrueNAS CORE live enclosures, including the primary top-loader and any
  separate front/back views.
- TrueNAS SCALE live enclosure using dynamic SES fallback.
- Quantastor HA shared SES views and inventory-bound storage views.
- Linux / GPU server storage views.
- VMware ESXi AOC / FatTwin read-only views.
- UniFi UNVR / UNVR Pro views.
- Saved `ses_enclosure` chassis views and custom-profile views.

## Admin Sidecar Sweep

Open `http://127.0.0.1:8082`:

- Confirm runtime cards show UI, history, and admin containers with live
  version and state.
- Click `Refresh State` and confirm it refreshes without changing config.
- Confirm `Runtime Behavior` fields show the correct ownership:
  `.env`-owned values disabled/read-only, admin-owned values editable.
- Change an admin-owned cache timing in a disposable local config only, save,
  confirm `runtime-overrides.yaml` is updated, then revert to the intended
  value before release.
- Walk the saved systems list for every platform and confirm secrets remain
  masked.
- Open setup/edit for each platform type and confirm platform-specific fields
  are present or disabled as expected.
- Exercise SSH key listing/refresh and sudoers preview without writing to
  production systems.
- Open profile builder, saved profiles, storage-view previews, and maintenance
  panels.
- Export a system backup/debug bundle and confirm `runtime-overrides.yaml` is
  included in the non-secret backup path list.
- Do not import, restore, purge orphaned data, or adopt removed history against
  real state during the RC pass.

## History Sidecar Sweep

Open `http://127.0.0.1:8081`:

- Confirm the dashboard loads quickly with estimated counts by default.
- Confirm `Collector Status` shows last collection duration and whether the
  last pass used cached or forced inventory.
- After a fresh sidecar start, confirm the startup/background fast pass stays
  cached-root-only for the current root scope: `cached_root_only=true`,
  `last_collection_inventory_forced=false`, and `last_scope_count=1`. A later
  scheduled slow/full pass may legitimately show forced inventory and all
  saved-system enumeration; it should still keep `/healthz` and the dashboard
  responsive while exposing stage timings for the slow work.
- Confirm `Collector Status`, the top count cards, `DB Size`, and `Tracked
  Scopes` update without using browser refresh. The banner should follow
  `/healthz` within a few seconds, and the count/table view should follow the
  sidecar overview poll.
- Save at least one `/healthz` payload from a running collection and one after
  completion, including `collection_stage_timings`, so slow paths can be
  reviewed without guessing.
- Click `Refresh Fast`; confirm it completes and the dashboard updates in
  place with fresh collector timestamps. Fast refresh should report cached
  inventory unless the environment explicitly sets
  `HISTORY_FORCE_INVENTORY_ON_FAST_COLLECTION=true`.
- If fast refresh hits a cold or unavailable cached SMART batch, confirm the
  delay is bounded to roughly `5 s`, the stage timings include `smart.failed`,
  and `last_error` remains empty. That path is a tolerated cache miss, not a
  full collector failure.
- Click `Refresh Full`; expect a slower SMART-heavy pass, then confirm slow
  metric timestamp changes, forced inventory, and a responsive activity banner
  while the pass is running.
- If a recent backup exists inside `HISTORY_BACKUP_INTERVAL_SECONDS`, confirm
  the stage timings show `db.backup.skipped` instead of another large DB copy.
  If no recent backup exists, confirm `db.backup` completes and records its
  duration.
- Query the exact path deliberately and record timing:

```powershell
Measure-Command { curl.exe -fsS "http://127.0.0.1:8081/api/history/overview?exact_counts=true" | Out-Null }
```

- Confirm the default overview path remains fast:

```powershell
Measure-Command { curl.exe -fsS "http://127.0.0.1:8081/api/history/overview" | Out-Null }
```

## Snapshot Export Sweep

For at least one large live enclosure and one storage-view style target:

- Open the export dialog and confirm the estimate appears once.
- Change packaging from `Auto` to `Force ZIP`; confirm the UI reuses the
  existing estimate instead of recalculating just because packaging changed.
- Download after that packaging change and confirm the response timing/header
  stages do not show a fresh snapshot/SMART reload; the artifact build should
  use the source inputs staged by the estimate.
- Export plain HTML when under the limit.
- Export forced ZIP.
- Export with redaction on.
- Open each artifact offline and confirm:
  - no live actions are available
  - search and slot click work
  - preloaded history opens without lazy-placeholder regressions
  - slot details keep readable persistent IDs

## Visual Acceptance Pass

Run this after the functional smoke passes and before refreshing release
screenshots or wiki images. The goal is to catch real operator-facing layout
problems, not to re-litigate already accepted geometry.

Use the live browser at normal zoom and capture screenshots under the evidence
folder for:

- main UI at desktop widths around `1920x1080` and `1366x768`
- one narrower viewport wide enough to expose wrapping problems in the toolbar,
  timing chips, sidebars, and admin tables
- history dashboard idle, history dashboard while collection is running, and a
  populated main-UI history drawer
- admin runtime cards, runtime behavior ownership, setup/edit for each platform
  family, profile builder, storage-view previews, backup/export, and host-prep
  panels
- saved `ses_enclosure` chassis views, live top-loader/front-back views,
  storage-view shells, ESXi AOC/FatTwin views, Quantastor HA views, and one
  exported/offline snapshot

For each screenshot, check:

- no labels, chips, buttons, tables, or drawers overlap
- the refresh/countdown strip wraps cleanly without hiding controls
- profile-driven tray/latch/LED/row geometry is consistent between live, saved,
  admin preview, builder preview, storage-view preview, and offline export
- empty/unknown/stale states look intentional rather than broken
- warnings remain visible but do not bury the primary enclosure view
- copy buttons, history buttons, refresh controls, and LED controls remain
  reachable at the tested widths

If the visual pass finds layout regressions, fix and rerun the relevant
functional smoke before refreshing release screenshots.

## Optional-Sidecar Runtime Modes

Validate these modes inside the disposable QA stacks, not against the
long-running dev stacks:

- UI only: stop admin/history, confirm main UI still works and history shows a
  clean unavailable state.
- UI + history: start history only, confirm main UI history paths work and the
  history dashboard refresh buttons work.
- UI + admin: start admin only, confirm runtime cards handle the missing
  history container cleanly.
- Full stack: all services running and version-aligned.

## Performance Harness

Run the read-only harness after functional smoke passes:

```powershell
.\.venv\Scripts\python.exe scripts\run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --skip-mappings-import-roundtrip --format markdown --label v0.17.0-rc-local-windows
.\.venv\Scripts\python.exe scripts\run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label v0.17.0-rc-history-local-windows
.\.venv\Scripts\python.exe scripts\run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 1 --include-exact-counts --format markdown --label v0.17.0-rc-history-local-windows-exact
```

Run the equivalent Linux command against the Linux stack:

```bash
python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --skip-mappings-import-roundtrip --format markdown --label v0.17.0-rc-codex-dev-test-linux
python scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label v0.17.0-rc-history-codex-dev-test-linux
python scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 1 --include-exact-counts --format markdown --label v0.17.0-rc-history-codex-dev-test-linux-exact
```

If snapshot export estimate is still a release risk, add a focused run with the
current large enclosure selected and save the browser/network timing notes next
to `data/perf/latest.md`. Save history sidecar timing notes next to
`data/history-perf/latest.md`; the exact-count history run is intentionally
separate so a slow SQLite count does not hide the normal dashboard polling
latency.

## Release Docs And Wiki Audit

Do this only after the RC matrix and visual pass are clean enough that the UI
wording is unlikely to churn again.

- Update `CHANGELOG.md` with the final `v0.17.0` section.
- Add `docs/RELEASE_NOTES_0.17.0.md` and `docs/RELEASE_WRAP_0.17.0.md`.
- Review `README.md` for stale version, screenshot, GHCR, or milestone wording.
- Review the checked-in `wiki/` pages for the same stale wording, especially:
  - `wiki/Quick-Start.md`
  - `wiki/Docker-and-GHCR-Deployment.md`
  - `wiki/Admin-UI-and-System-Setup.md`
  - `wiki/Live-Enclosures-and-Storage-Views.md`
  - `wiki/History-and-Snapshot-Export.md`
  - `wiki/Troubleshooting.md`
- Make sure docs explain the operator-visible `0.17.0` work that actually
  shipped: shared enclosure/profile rendering, runtime behavior ownership,
  refresh/cache timing chips, history dashboard live updates, history fast/full
  refresh behavior, metrics/Grafana status, and backup/restore portability.
- Decide whether the release needs refreshed screenshots. Because `0.17.0`
  changes operator-facing geometry, admin/runtime surfaces, and history
  dashboard behavior, default to refreshing screenshots unless the visual pass
  proves the existing set is still accurate enough and the release notes record
  that decision.
- When refreshing screenshots, run the existing capture scripts with the target
  release tag:

```powershell
$env:SCREENSHOT_TAG = "v0.17.0"
.\.venv\Scripts\python.exe scripts\capture_readme_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_history_export_screenshots.py
.\.venv\Scripts\python.exe scripts\capture_release_workflow_screenshots.py
```

- Verify new screenshots under `docs/images/screenshots/` and `wiki/images/`
  before staging them.
- If the checked-in `wiki/` tree changes, sync and push the separate GitHub wiki
  repo after the release commit is ready.

## Evidence Capture

Each environment folder should contain:

- `git-status.txt`
- `docker-ps.txt`
- `docker-logs-ui.txt`, `docker-logs-history.txt`, and `docker-logs-admin.txt`
  captured with `docker logs --timestamps --tail 2000` after the slow live
  sweeps
- if a manual history refresh fails, save the refresh response body and the
  matching `docker-logs-history.txt` / `docker-logs-ui.txt` window; the history
  and admin sidecars log to container stdout/stderr and optional Docker syslog,
  while the main UI also keeps the rotating `logs/app.log`
- `livez-healthz.json`
- verify the history `/healthz` payload includes
  `background_consecutive_failures`, `background_backoff_until`,
  `background_backoff_seconds_remaining`, and `next_collection_at`; after a
  healthy pass the failure count and remaining backoff should be `0`
- while history collection is running, verify `/healthz` stays responsive and
  reports `collection_kind`, `collection_activity`, and
  `collection_elapsed_seconds`; the dashboard should show the live activity
  banner and a `DB Size` card
- `admin-state.json`
- `history-overview.json`
- `history-overview-exact-timing.txt`
- `metrics-ui.txt`, `metrics-history.txt`, and `metrics-admin.txt`
- `perf-latest.md`
- `history-perf-latest.md`
- screenshots for main UI, admin, history, export dialog, offline artifact, and
  at least one populated slot history drawer
- screenshots and notes from the visual acceptance pass
- `docs-audit.md` listing docs/wiki/screenshot decisions and any intentionally
  deferred wording updates
- a short `notes.md` listing systems covered, skipped systems, failures, and
  whether any LED was toggled

## Browser Automation Follow-Up

The existing Playwright suite should stay fast enough for normal CI-style local
runs. The RC pass can add a slower optional harness later if needed:

- enumerate saved systems from `/api/inventory`
- visit each system/enclosure/view
- click representative slot classes
- exercise copy buttons and history drawer
- export one snapshot estimate
- collect console errors, screenshots, and API timings

If this grows into code, keep it opt-in and artifact-heavy, for example:

```powershell
npx playwright test qa/v0_17_rc_matrix.spec.js --project=chromium
```

## Go / No-Go

Do not cut the release if any of these are true:

- a live system renders a materially wrong slot layout or disk identity
- any safe LED identify test fails to turn off cleanly
- history refresh buttons fail or leave the sidecar stuck running
- write/change/destructive/failure-mode testing was run against the
  long-running dev stacks or real state instead of disposable QA Docker stacks
- snapshot export repeats the forced-ZIP estimate regression or the download
  path reloads snapshot/SMART inputs after an estimate when only packaging
  changed
- copy buttons throw browser console errors on local HTTP
- admin/runtime ownership shows editable `.env`-owned behavior values
- metrics are enabled but any service lacks a working `/metrics` endpoint
- the checked-in Grafana dashboards show query errors against the RC scrape
  target without a documented datasource/environment reason
- release-facing README/wiki/screenshots are stale after an operator-visible UI
  or workflow change
- Linux and Windows disagree on basic live inventory shape for the same saved
  system without a documented environmental reason

Minor visual differences, slow exact-count history queries, and unavailable
platform-specific tools can ship only if they are documented in the final RC
notes and do not hide real operator risk.
