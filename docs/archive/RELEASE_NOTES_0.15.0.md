# Release Notes - v0.15.0

Release date: `2026-04-27`

## Summary

`0.15.0` turns the current ESXi slice into a broader Supermicro BMC / IPMI
release.

The big change is that validated FatTwin nodes no longer have to pretend ESXi
is the primary source of physical truth. The app can now use the Supermicro
BMC as the hardware-facing inventory and LED path, then let ESXi add
controller, StorCLI, and host SMART enrichment where that is available.

This release also rounds out the operator workflow around that hardware:

- a real `ipmi` platform path
- first-pass Supermicro FatTwin front/rear profiles
- BMC-backed slot identify and chassis locator APIs
- ESXi `Password Only / No Key` setup support
- operator-supplied ESXi host prep for StorCLI bundles
- a much tighter README/wiki/deploy story around published images and optional
  sidecars

## Added

- `ipmi` as a first-class saved platform for Supermicro BMC-driven inventory
- Supermicro Broadcom storage inventory via Redfish where available, with the
  validated web XML fallback kept as the no-extra-license path
- built-in `Supermicro FatTwin Front 6` and inferred `Rear 2` profiles/views
- BMC-backed slot identify plus `/api/system-locator` backend support for the
  node/chassis UID light
- ESXi `Host Prep / Vendor Tool Upload` in the admin sidecar for
  operator-supplied StorCLI `.zip` / `.vib` packages

## Changed

- the default operator compose path is now `docker-compose.yml`, while
  `docker-compose.dev.yml` is the explicit source-build path
- history and admin sidecars are now documented as normal supported optional
  runtime services, not as dev-only add-ons
- the README is much shorter and more front-page-oriented, with the wiki
  carrying the deeper operator detail
- ESXi slot detail/history wording now reflects direct `JBOD` members, generic
  controller-backed logical devices, and enclosure-first topology ordering more
  honestly

## Fixed

- FatTwin front-slot numbering now follows the validated Supermicro bottom-up
  then left-to-right order: `02 05 / 01 04 / 00 03`
- BMC-good ESXi slots no longer fall through to `UNKNOWN` when StorCLI or host
  SMART enrichment is temporarily absent
- ESXi warning paths now explain whether StorCLI is missing, the controller is
  hidden by passthrough, or the tool is installed but still not surfacing a
  controller
- ESXi SMART counters no longer label generic host `Read Error Count` data as
  `Uncorrected Read`

## Validation Snapshot

Validated on `codex/v0.15.0-kickoff-2026-04-26-post-0.14.2`.

Local Windows Docker:

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  -> `317` tests passed
- `npx playwright test` -> `15` passed, `1` skipped
- `python -m compileall app admin_service history_service tests`
- `node --check app/static/app.js`
- `node --check admin_service/static/admin.js`
- `node --check qa/admin-operations.spec.js`
- `node --check qa/esxi-smoke.spec.js`
- rebuilt stack returned `0.15.0` on:
  - `GET /livez`
  - `GET :8081/livez`
  - `GET :8082/livez`

Linux dev target (`codex-dev-test-target`):

- the earlier wrap validation on the same feature set stayed clean there, with
  syntax/compile checks passing and the rebuilt stack aligning normally
- perf remained the representative baseline on Linux-hosted Docker:
  - `health_cached` about `3.2 ms`
  - `inventory_cached` about `7.4 ms`
  - `storage_views_cached` about `32.6 ms`
  - `history_status` about `84.2 ms`
  - `snapshot_export_estimate` about `642.6 ms`

## Deployment Note

This release keeps the refreshed `v0.15.0` screenshot set and the newer
published-image deployment shape:

- `docker-compose.yml` is the normal pull-from-GHCR operator path
- `docker-compose.dev.yml` is the source-build path
- the history and admin sidecars remain optional, but they are documented as
  first-class citizens under the same published-image workflow

One known caveat remains outside the release-blocker bucket: local Windows
Docker Desktop still shows much slower `history_status` and
`snapshot_export_estimate` timings than the Linux dev target. That remains a
follow-up tuning issue, not a correctness regression in the `0.15.0` feature
set.
