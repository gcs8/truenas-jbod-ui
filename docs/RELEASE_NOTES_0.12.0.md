# Release Notes - v0.12.0

Release date: `2026-04-21`

## Summary

`0.12.0` is the builder mode, Linux host parity, and chassis-profile cleanup
release.

The goal of this release is to make the profile-driven chassis system feel
operator-usable instead of YAML-only. The optional admin sidecar now has a
dedicated `Enclosure / Profile Builder` workspace for cloning and saving custom
profiles, saved live-backed chassis views now follow the same profile-driven
geometry path as their live enclosure counterparts, and the Linux-hosted Docker
validation path no longer depends on a fake writable `/app/...` harness just
to run the repo outside containers.

## Highlights

- Builder mode is now a real operator workflow:
  - the admin sidecar exposes a dedicated `Enclosure / Profile Builder`
    workspace
  - operators can load a built-in profile, clone it into a reusable custom
    `profiles.yaml` entry, update it later, or delete it safely when nothing
    references it
  - the builder preview now has its own full-width panel so the draft geometry
    can be judged at a more realistic scale
- Slot ordering is no longer YAML-only:
  - generated `Slot Ordering` presets now cover the common row-major and
    column-major numbering patterns
  - a `Custom Matrix` editor can round-trip explicit `slot_layout` matrices
    such as `02 05 / 01 04 / 00 03`
  - explicit `slot_layout` payloads now fail early when the visible slot count
    does not match `slot_count`
- Saved live-backed chassis views now behave much more like the live enclosure
  they mirror:
  - empty-detail and click-off behavior now match the live path
  - profile-aware row-group geometry is shared so tray width, latch placement,
    and LED spacing stay aligned
- Linux is now treated as the primary deploy/validation shape for follow-up
  runtime and perf work:
  - host-side defaults now resolve relative to the real checkout/config path
  - legacy `/app/...` config values rebase cleanly on host-side runs
  - startup logging degrades to console-only when a Docker-owned log file is
    not writable
- Snapshot export estimate recovered materially after restoring the batched
  scope-history request path and tightening the history-sidecar SQLite
  temp-store/cache settings

## Operator Notes

- Builder mode is intentionally still preset-driven in this release. The app
  now supports reusable custom profiles, ordering presets, and explicit layout
  matrices, but a more visual tile-level editor is still deferred to later
  work.
- Cross-host backup/restore is still narrower than the eventual target. The
  current full backup bundle still does not include SSH keys, imported TLS
  trust material, or shared `known_hosts`; that remains a later encrypted-only
  export enhancement.
- Linux-hosted Docker is now the preferred validation target for follow-up perf
  work. Windows Docker Desktop still works, but bind-mounted SQLite I/O remains
  the more distortion-prone baseline for history-heavy workflows.

## Validation Snapshot

The final release-wrap validation pass confirmed:

- local Windows broad Python suite: `234` passing tests
- Linux dev target host-side broad Python suite: `234` passing tests
- local Windows browser QA: `9` passing / `1` skipped
- Linux dev target browser QA against `http://10.13.37.138:8080`: `10` passing
- local Windows perf harness (`release-candidate-0.12.0-local-windows`):
  - `inventory_force` avg `8315.8 ms`
  - `snapshot_export_estimate` avg `13988.8 ms`
  - `route.export_snapshot_estimate.load_smart_summaries` avg `10926.3 ms`
- Linux dev target perf harness (`release-candidate-0.12.0-linux-dev-target`):
  - `inventory_force` avg `4619.8 ms`
  - `snapshot_export_estimate` avg `476.9 ms`
  - `snapshot_export.collect_slot_histories` avg `850.2 ms`
  - `route.export_snapshot_estimate.load_smart_summaries` avg `63.0 ms`
- refreshed `v0.12.0` screenshot set in both:
  - `docs/images/screenshots/`
  - `wiki/images/`

The main A/B takeaway is that Linux is the preferred baseline for this
workload. Windows Docker Desktop still validates functionally, but the local
bind-mounted stack remains much slower for SMART-heavy snapshot-estimate paths.
The Linux history sidecar also showed a couple of post-restart inventory fetch
timeouts while warming against the imported live config, but it recovered to a
healthy state on its own before the perf run and the full remote browser matrix
still passed.

Run the final broad validation matrix again immediately before the actual cut:

- `docker compose up -d --build`
- final release commit/tag push flow
- external wiki publish if the checked-in `wiki/` tree changed after the last
  sync

## Checked-In Artifacts

Release-facing screenshot refresh completed for this cut:

- `docs/images/screenshots/*-v0.12.0.png`
- `wiki/images/*-v0.12.0.png`

Captured highlights for this release:

- dedicated builder workspace
- updated admin setup view
- refreshed grouped runtime selector state
- storage-view history and snapshot-export flow
- refreshed maintenance/history tools
- refreshed Quantastor HA SATADOM runtime view

## Deployment Notes

- App version for the release commit is `0.12.0`.
- Operators should re-review:
  - `README.md`
  - `docs/PROFILE_AUTHORING.md`
  - `wiki/Admin-UI-and-System-Setup.md`
  - `wiki/Profiles-and-Custom-Layouts.md`
  - `docs/RELEASE_CHECKLIST.md`
- Final release prep for this cut should still include:
  - a final `git status` / commit-shape review
  - the release version bump and `CHANGELOG.md` closeout
  - external wiki publish if the checked-in `wiki/` tree changed

## Suggested GitHub Release Intro

`0.12.0` turns the profile-driven layout system into a more usable operator
workflow. It adds a dedicated admin-side builder workspace for reusable custom
profiles, brings saved live-backed chassis views into much tighter geometry
parity with the live enclosure path, removes the awkward Linux host-side
`/app/...` harness assumption for repo-local runs, and recovers the
snapshot-export estimate path after restoring the intended batched history read
behavior.
