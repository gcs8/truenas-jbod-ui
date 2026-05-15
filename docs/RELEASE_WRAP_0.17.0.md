# Release Wrap - v0.17.0

Date: `2026-05-15`

## Scope

`0.17.0` is the shared-rendering, history-responsiveness, and runtime-control
cleanup release.

It does not add a new storage platform. It makes the current supported
platforms easier to trust by tightening how the same enclosure shape renders
across live views, saved chassis views, admin previews, builder previews,
storage-view previews, and offline snapshots.

## What This Release Locks In

- one profile-driven geometry path for `ses_enclosure` style faces across the
  main UI, admin setup, builder, storage-view previews, and exported snapshots
- clearer main-UI refresh behavior, with the global countdown separated from
  individual cache timing chips
- a live history sidecar dashboard that updates collector state and counts
  without requiring an F5
- explicit fast/full history refresh controls with stage telemetry that shows
  cached versus forced inventory behavior
- bounded fast-path cached SMART misses and skip/continue behavior for
  isolated full-collection failures
- snapshot export estimate/download reuse for packaging-only changes, so a
  forced ZIP download does not re-pull snapshot/SMART inputs after the estimate
- admin-owned runtime behavior overrides that are safe to edit in the sidecar,
  while `.env` owned values remain read-only
- disposable Windows/Linux restore QA stacks as the release gate for import,
  restore, mutation, destructive cleanup, and sidecar failure-mode checks

## Validation

Local Windows Docker:

- `compileall` passed for app/admin/history/tests/scripts
- full unit discovery passed with `379` tests; the later focused export-source
  cache and backup-pruning fixes are covered by full `pytest` at `380` tests
- Playwright passed with `22` tests
- raw UI/history/admin `/metrics` scrapes were captured
- manual fast history refresh stayed cached-root-only and completed with a
  bounded cached-SMART miss
- manual full history refresh returned `200` and skipped one slow SMART target
  instead of leaving the sidecar failed
- disposable restore QA stack restored the backup, changed runtime behavior,
  created/deleted a QA-only demo system, exercised orphan history cleanup,
  stopped/started UI/history containers, exported a mutated backup, and
  re-imported it

Linux dev target:

- VM was rebooted after OOMs and came back healthy
- long-running public UI/history/admin were rebuilt and reported `0.17.0-dev`
  during RC on `10.13.37.138:8080`, `:8081`, and `:8082`
- the public stack was rebuilt again after the late export-source-cache fix;
  `qa/ui-switching.spec.js` passed against `:8080`, and direct Archive CORE
  estimate followed by forced ZIP download returned from the staged inputs
- disposable alternate-port RC stack passed Python, Playwright, perf, and
  teardown checks
- disposable restore QA stack repeated the backup restore, QA-only mutation,
  failure-mode stop/start, mutated export, and re-import drill
- random populated-disk identify checks passed on every LED-capable
  system/enclosure, with a final forced-refresh sweep confirming all tested
  slots were off

## What Still Rolls Forward

- final release mechanics: version bump, release commit, tag, GitHub release,
  GHCR verification, and external wiki sync
- later decision on whether the comprehensive RC checklist should become a
  scripted artifact-heavy harness instead of a documented manual gate
