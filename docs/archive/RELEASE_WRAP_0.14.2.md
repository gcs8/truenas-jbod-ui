# Release Wrap - v0.14.2

Date: `2026-04-26`

## Status

`0.14.2` is the small ESXi/runtime-clarity follow-up release on top of the
`0.14.1` hotfix baseline.

The release scope stays intentionally tight:

- ESXi-capable `Platform Details`
- live running-version visibility on all three UI surfaces
- admin runtime-card version drift visibility
- light release-doc/wiki true-up for the new stable tag

## What This Release Locks In

- read-only ESXi carrier views now have a dedicated home for non-standard
  controller / virtual-drive / datastore / member-capability context that does
  not interfere with the slot-details rail
- operators no longer need to infer the running build from snapshot/export
  artifacts; the main UI, history dashboard, and admin sidecar all show the
  active app version directly
- admin runtime maintenance now makes container skew visible in one place by
  comparing the live `/livez` version payloads from UI/history/admin
- latest-release checks are cached and backgrounded instead of turning normal
  page loads into GitHub traffic

## Validation

Local Windows Docker:

- `283` Python tests passed
- Playwright smoke passed with `15` green / `1` skipped
- syntax/compile checks passed for Python plus the main/admin/QA JS surfaces
- local warmed perf kept cached read paths reasonable, while the existing
  Windows-only pain still clusters around `history_status`, `inventory_force`,
  and the first slot-history-rich snapshot-estimate pass

Linux dev target:

- syntax/compile checks passed
- rebuilt stack reported `0.14.2` on `/livez`
- admin runtime cards reported UI/history/admin aligned on `0.14.2`
- perf stayed clean and representative there:
  - `health_cached` about `3.2 ms`
  - `inventory_cached` about `7.4 ms`
  - `history_status` about `84.2 ms`
  - `snapshot_export_estimate` about `642.6 ms`

## What Still Rolls Forward

- broader "API really optional" work beyond the already SSH-first `linux` /
  `esxi` families
- the still-noisy local Windows Docker Desktop history/export perf path
- tuning the history sidecar's full-fleet collector pass on wide saved installs
  where slower hosts can exceed the current inventory request timeout during
  background collection
- the larger `0.15.0-dev` cleanup queue already tracked in `TODO.md`
