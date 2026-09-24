# Release Notes - v0.14.1

Release date: `2026-04-26`

## Summary

`0.14.1` is a narrow hotfix for SSH-only system setup in the admin sidecar.

The `0.14.0` ESXi release shipped the right runtime behavior, but the setup
form still enforced a TrueNAS/API-style host requirement even when the target
platform was intentionally SSH-first. This hotfix removes that mismatch for the
already SSH-only `esxi` and generic `linux` platform families, which also
covers the UniFi installs that ride on the generic Linux path here.

## Fixed

- admin system setup now allows SSH-only `esxi` entries to save with only the
  SSH host populated
- admin system setup now does the same for generic `linux` entries, including
  UniFi-family hosts that use the Linux adapter path
- when the API host box is blank on those SSH-only platforms, the save path now
  normalizes `ssh_host` into the saved primary host field instead of blocking
  the request

## Validation Snapshot

Validated on `codex/v0.14.1-hotfix-prep-2026-04-26`:

- `node --check admin_service/static/admin.js`
- `.\.venv\Scripts\python.exe -m unittest tests.test_system_backup tests.test_admin_service -v`
- result: `59` tests passed

## Deployment Note

This hotfix does not refresh the `v0.14.0` screenshot set because the shipped
UI surface itself did not materially change. The release-facing GHCR examples
now point at `v0.14.1` as the latest stable image tag.
