# Release Wrap - v0.14.1

Date: `2026-04-26`

## Status

`0.14.1` is a narrow hotfix cut for the SSH-only admin setup save path.

The branch only carries the targeted fix needed to let `esxi` and generic
`linux` systems save cleanly without inventing an API host first, plus the
small release-metadata/doc updates needed to ship that fix.

## What This Hotfix Locked In

- SSH-only `esxi` setup now saves cleanly when the operator fills only the SSH
  host
- generic `linux` / UniFi-family setup now gets the same SSH-first save path
- the backend request model now matches the admin form instead of rejecting the
  normalized SSH-only payload later in the request chain

## Validation

- `node --check admin_service/static/admin.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_system_backup tests.test_admin_service -v`
- `59` tests passed

## What Still Rolls Forward

- deciding whether API host/auth should become more broadly optional beyond the
  already SSH-only `linux` / `esxi` families
- the larger `0.15.0-dev` cleanup queue already tracked in `TODO.md`
