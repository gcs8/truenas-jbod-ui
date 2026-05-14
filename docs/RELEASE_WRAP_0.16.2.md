# Release Wrap - v0.16.2

Date: `2026-05-14`

## Scope

`0.16.2` is a patch release on top of `0.16.1` for live operator feedback in
the enclosure detail rail and snapshot export dialog.

The runtime changes are intentionally small:

- export dialog packaging and oversize changes reuse the loaded estimate
- TrueNAS `{serial_lunid}` fallback identifiers no longer masquerade as GPTID
  values when stronger zpool GPTID data exists
- copy buttons keep working on browsers where LAN HTTP does not expose
  `navigator.clipboard.writeText`

## Shipped Files

- `app/services/inventory.py`
- `app/static/app.js`
- `qa/ui-switching.spec.js`
- `tests/test_inventory.py`
- `app/__init__.py`
- `package.json`
- `package-lock.json`
- `CHANGELOG.md`
- `docs/RELEASE_NOTES_0.16.2.md`
- `docs/RELEASE_WRAP_0.16.2.md`
- checked-in wiki/GHCR deployment pages with pinned-image examples updated to
  `v0.16.2`

## Validation

- `node --check app/static/app.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_inventory tests.test_snapshot_export -v` (`117` tests)
- `.\.venv\Scripts\python.exe -m compileall app tests`
- `npx playwright test qa/ui-switching.spec.js -g "export snapshot dialog renders estimate UI"`
- `docker compose up -d --build enclosure-ui`
- `GET http://127.0.0.1:8080/livez` returned `{"status":"ok","version":"0.16.2"}`
- `git diff --check`

## Notes

No screenshot refresh is needed for this patch because the shipped UI layout did
not materially change. Operators should prefer fresh exports generated from
`0.16.2` or later when they want the cheaper packaging-selection path and the
copy-button fallback in the inlined viewer JavaScript.
