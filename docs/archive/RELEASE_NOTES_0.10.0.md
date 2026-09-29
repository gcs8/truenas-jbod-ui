# Release Notes - v0.10.0

Release date: `2026-04-19`

## Summary

`0.10.0` is the admin, storage-view, and recovery release.

The goal of this release is to make the project easier to operate as a real
tool instead of just a read-only enclosure visualizer: system setup, backup
and restore, SSH/TLS trust workflows, saved storage views, and internal-disk
groups are now part of the checked-in product story rather than side notes.

## Highlights

- Dedicated optional admin sidecar for:
  - guided system setup
  - runtime start/stop/restart control
  - SSH key reuse and generation
  - TLS certificate inspection and trust import
  - full config plus history backup/export and restore
- Saved storage views now behave like first-class runtime targets instead of
  second-class metadata:
  - the main UI groups `Live Enclosures`, `Saved Chassis Views`, and
    `Virtual Storage Views`
  - inventory-bound views like `Boot SATADOMs` and the NVMe carrier now have
    dedicated SMART and history runtime paths
- Saved chassis views now persist their own profile choice, so a curated
  chassis overlay can keep its own layout instead of drifting with whichever
  live enclosure profile is currently selected
- The admin add-flow for storage views now uses one grouped picker and hides
  saved chassis layouts that would just duplicate already-discovered live
  hardware on the loaded system
- `archive-core` now exposes the separate CORE `Front 24 Bay` "brain" chassis
  as a live discovered enclosure alongside the combined `60`-bay shelf
- The optional history sidecar is more resilient on the validated Docker
  Desktop bind-mount path, including readonly-database repair and permission
  normalization for the SQLite store

## Operator Notes

- The project now has a clearer runtime mental model:
  - `Live Enclosures` are discovered hardware
  - `Saved Chassis Views` are optional curated overlays
  - `Virtual Storage Views` are internal groups such as NVMe carrier cards or
    boot-device pairs
- On the validated CORE host `The-Archive`, the `jbodmap` service account now
  works cleanly with a command-limited `sudo_nopasswd=true` allow-list instead
  of relying on a stored sudo password for normal SMART and LED workflows.
- `archive-core` still keeps the combined `60`-bay enclosure as the default
  live selector option; the separate `Front 24 Bay` view is a peer live
  enclosure, not a replacement.
- Quantastor `verify_ssl: false` now truly disables certificate verification on
  the REST path instead of failing through the verified transport anyway.

## Validation Snapshot

Latest validated checkpoints recorded during the `0.10.0` wrap-up pass:

- Branch-tip code validation on `2026-04-19`:
  - `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  - Result: `195` passed
- Follow-up focused validation on `2026-04-19`:
  - `.\.venv\Scripts\python.exe -m unittest tests.test_admin_service tests.test_inventory tests.test_system_backup -v`
  - Result: `96` passed
  - `.\.venv\Scripts\python.exe -m unittest tests.test_admin_service -v`
  - Result: `15` passed
  - `node --check admin_service/static/admin.js`
  - `node --check app/static/app.js`
- Browser QA matrix on `2026-04-19`:
  - full stack (`enclosure-ui + enclosure-history + enclosure-admin`):
    `8` passed, `1` skipped
  - standalone UI only (`enclosure-ui`, with admin/history stopped):
    `8` passed, `1` skipped
  - UI plus history (`enclosure-ui + enclosure-history`, with admin still
    stopped): `8` passed, `1` skipped
  - storage-view history browser coverage now also accepts the supported
    standalone case where the History control stays hidden because the history
    backend is intentionally unavailable
- Live runtime checks recorded on `2026-04-19`:
  - `archive-core` inventory now returns two live enclosure options
  - the separate `Front 24 Bay` live enclosure resolves to the
    `supermicro-ssg-6048r-front-24` profile with `24` visible slots
  - storage-view history for `Boot SATADOMs` and the NVMe carrier is returning
    populated samples again after the history-store/runtime fix
- Final post-QA perf profile on `2026-04-19`:
  - `python scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate-0.10.0-final-post-qa`
  - latest summary:
    - `inventory_force` avg `4926.3 ms`
    - `inventory_cached` avg `18.5 ms`
    - `smart_batch` avg `12.7 ms`
    - `mappings_import_roundtrip` avg `1107.9 ms`
    - `snapshot_export_estimate` avg `7145.8 ms`

## Checked-In Artifacts

Release-facing screenshots were refreshed on `2026-04-19` under:

- `docs/images/screenshots/*-v0.10.0.png`
- `wiki/images/*-v0.10.0.png`

New workflow captures for this release include:

- grouped runtime selector state on `archive-core`
- storage-view history on `Boot SATADOMs`
- the separate CORE `Front 24 Bay` live enclosure
- the admin sidecar grouped `Add Storage View` workflow

The current `archive-core` runtime screenshot intentionally shows live plus
virtual groups only, because the validated config no longer ships with a
duplicate saved chassis view enabled by default. The paired admin capture shows
the saved chassis layout catalog that operators can add when they actually need
one.

## Deployment Notes

- App version is now `0.10.0` in the release snapshot.
- Existing operators should re-review:
  - `.env.example`
  - `config/config.example.yaml`
  - `config/profiles.example.yaml`
  - `docs/SSH_READ_ONLY_SETUP.md`
- Release prep should include:
  - the broad Python suite
  - Playwright browser QA
  - a fresh `docker compose up -d --build`
  - a quick live sweep of the validated platform views and the new admin-side
    workflows that are operator-facing in this release

## Suggested GitHub Release Intro

`0.10.0` turns the JBOD UI into a much more complete operator tool. It adds a
dedicated admin sidecar for setup, SSH/TLS trust, backup/restore, and runtime
control; makes saved storage views first-class in the main UI; surfaces the
separate CORE `24`-bay brain chassis on `archive-core`; and hardens the
optional history path so internal NVMe and SATADOM views keep their SMART and
history workflows instead of feeling bolted on.
