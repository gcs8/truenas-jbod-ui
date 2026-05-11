# Release Notes - v0.16.1

Release date: `2026-05-11`

## Summary

`0.16.1` is a narrow hotfix for offline enclosure snapshot exports.

`0.16.0` preserved slot history inside self-contained HTML snapshot artifacts,
but the browser lookup path started checking the live, windowed cache key first.
Live pages could recover by fetching from the history sidecar. Frozen offline
artifacts cannot do that by design, so an exported snapshot with the history
drawer open could stay on the lazy-load placeholder even though the artifact
contained the history samples.

This patch restores the offline viewer path without changing the live history
cache behavior.

## Fixed

- offline snapshot exports now render preloaded slot history from the embedded
  artifact data when the history drawer is open
- snapshot-mode history lookup now falls back to the exported stable
  `system|enclosure|slot` cache key, while live mode keeps using the windowed
  cache key and fetch path
- browser QA now includes a generated self-contained offline snapshot regression
  test so this exact path is covered without a live backend

## Validation Snapshot

Validated on `codex/v0.16.1-offline-snapshot-history-2026-05-11`:

- `node --check app/static/app.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_snapshot_export -v`
- `npx playwright test ./qa/offline-snapshot.spec.js`
- `.\.venv\Scripts\python.exe -m compileall app tests`
- `git diff --check`

## Deployment Note

This hotfix does not refresh the `v0.16.0` screenshot set because the shipped UI
layout and workflows did not materially change. Fresh snapshot exports created
from `0.16.1` contain the fixed inlined viewer JavaScript; already-downloaded
HTML artifacts created by older builds remain frozen with their original code.
