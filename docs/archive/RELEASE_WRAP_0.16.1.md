# Release Wrap - v0.16.1

Date: `2026-05-11`

## Scope

`0.16.1` is a patch release on top of `0.16.0` for the offline snapshot
history viewer regression.

The only runtime behavior change is in the self-contained snapshot artifact:
when the artifact opens with history visible, the browser can now find the
embedded preloaded history payload using the stable exported cache key. Live
history reads continue to use their windowed cache and sidecar fetch behavior.

## Shipped Files

- `app/static/app.js`
- `qa/offline-snapshot.spec.js`
- `app/__init__.py`
- `package.json`
- `package-lock.json`
- `CHANGELOG.md`
- `docs/RELEASE_NOTES_0.16.1.md`
- `docs/RELEASE_WRAP_0.16.1.md`
- checked-in wiki/GHCR deployment pages with pinned-image examples updated to
  `v0.16.1`

## Validation

- `node --check app/static/app.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_snapshot_export -v`
- `npx playwright test ./qa/offline-snapshot.spec.js`
- `.\.venv\Scripts\python.exe -m compileall app tests`
- `git diff --check`

## Notes

No screenshot refresh is needed for this patch because the user-facing layout
did not change. Existing downloaded snapshot HTML files remain immutable; the
fix applies to fresh exports generated from this release or later.
