# Release Notes - v0.14.2

Release date: `2026-04-26`

## Summary

`0.14.2` is a small operator-facing follow-up release built around the new ESXi
read path and around live deployment clarity.

The release adds a capability-driven `Platform Details` panel for non-standard
platform context such as the read-only ESXi RAID-carrier view, surfaces the
running app version directly on the main/history/admin pages, and extends the
admin runtime cards so they can show per-container running/latest versions and
real cross-container drift.

## Added

- a collapsed `Platform Details` panel under the enclosure canvas for
  capability-driven controller / virtual-drive / datastore / member-capability
  context without crowding the slot-details rail
- live version display on the main UI, the history dashboard, and the admin
  sidecar
- admin runtime-card version probes so `Read UI`, `History Sidecar`, and
  `Admin Sidecar` each show:
  - the running version reported by that container's `/livez` payload
  - the latest tagged stable release
  - whether the running containers agree or drift

## Changed

- latest-release checks now run in the background at startup and then daily,
  using a shared cached payload instead of checking GitHub on every page load
- the release checklist now explicitly allows minor runtime/guardrail releases
  to keep the current screenshot set intentionally when the operator workflow
  visuals have not materially changed

## Validation Snapshot

Validated on `codex/v0.14.2-release-prep-2026-04-26`.

Local Windows Docker:

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  -> `283` tests passed
- `npx playwright test` -> `15` passed, `1` skipped
- `node --check app/static/app.js`
- `node --check admin_service/static/admin.js`
- `node --check qa/admin-operations.spec.js`
- `node --check qa/esxi-smoke.spec.js`
- `.\.venv\Scripts\python.exe -m compileall app admin_service history_service tests`
- rebuilt stack returned `0.14.2` on:
  - `GET /livez`
  - `GET :8081/livez`
  - `GET :8082/livez`
- warmed perf harness (`release-candidate-0.14.2-local-warm`) kept cached read
  paths in the expected band:
  - `health_cached` avg `7.6 ms`
  - `inventory_cached` avg `27.1 ms`
  - `storage_views_cached` avg `46.3 ms`
  - while the known Windows secondary-baseline costs remained concentrated in
    `history_status`, `inventory_force`, and `snapshot_export_estimate`

Linux dev target (`codex-dev-test-target`):

- `python3 -m compileall app admin_service history_service tests`
- `node --check app/static/app.js`
- `node --check admin_service/static/admin.js`
- rebuilt stack returned healthy UI/admin endpoints and aligned runtime-card
  versions on `0.14.2`
- perf harness (`release-candidate-0.14.2-linux`) stayed strong:
  - `health_cached` avg `3.2 ms`
  - `inventory_cached` avg `7.4 ms`
  - `storage_views_cached` avg `32.6 ms`
  - `history_status` avg `84.2 ms`
  - `snapshot_export_estimate` avg `642.6 ms`
  - `inventory_force` avg `6305.0 ms`

## Deployment Note

This release intentionally keeps the checked-in `v0.14.0` screenshot set in
place. The live operator workflows did not change enough to justify a full
media refresh for this cut, and the current screenshots still match the shipped
runtime story more cleanly than a prerelease version-badge recapture would have.

One Linux dev-target caveat did surface during validation: the history
sidecar's background collector stayed degraded there because the full saved
fleet includes a few systems whose forced inventory pass currently exceeds the
sidecar's request timeout. The read UI, history API endpoints, and admin
runtime cards still validated normally on that host, so this is tracked as a
follow-up tuning concern rather than as a `0.14.2` release blocker.
