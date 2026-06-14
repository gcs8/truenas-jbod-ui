# Release Notes - v0.21.2

Date: `2026-06-13`

`v0.21.2` is a narrow maintenance patch on top of the published `v0.21.1` release. It keeps the `v0.21.1` presence-flap history-noise fix and adds admin-state credential redaction hardening plus repository hygiene needed before reopening the broader `v0.22.0-dev` performance/guardrail cycle.

## Changed

- Added report-only coverage, agent entrypoint guidance, CodeQL, and Dependabot configuration so pull requests and `main` get stronger automated safety signals without turning coverage into a release blocker.
- Kept the open dependency-bump PRs out of this release scope; they remain separate maintenance work unless explicitly included later.

## Fixed

- Admin setup state no longer returns saved API keys, API passwords, SSH passwords, SSH sudo passwords, or BMC passwords to the browser.
- Existing systems can still be edited without retyping configured secrets through save-only preserve semantics; helpers that probe/bootstrap/discover systems continue to receive only the current form values.
- The admin setup bootstrap now uses script-safe JSON escaping so saved labels or hosts cannot break out of the bootstrap script block.
- Regression coverage now checks redacted state payloads, preserve-on-edit behavior, explicit secret replacement, save-only sentinel boundaries, and script-breakout-safe rendering.

## Validation

The release candidate passed the pre-tag validation stack:

- `.venv/bin/python -m compileall -q app admin_service history_service scripts tests`
- `.venv/bin/python -m unittest tests.test_admin_service tests.test_account_bootstrap tests.test_system_backup -q` — `103` tests passed
- `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` — `516` tests passed, `4` skipped
- `npm ci --ignore-scripts` — passed with `0` vulnerabilities
- `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js`
- `.venv/bin/python scripts/check_public_demo_artifact.py public-demo` — public demo artifact publishability check passed
- `git diff --check`
- Local Docker release-candidate health on `19080/19081/19082`
- Optional-sidecar runtime matrix
- Full Playwright browser smoke on the local candidate and restored Linux QA stack
- Live admin-state redaction probes on the local candidate and restored Linux QA stack
- Local and restored Linux QA perf harnesses
- Disposable Linux QA restore/provenance check against the approved full-data source
- Snapshot/export/offline browser smoke for local and restored Linux QA artifacts

The pre-tag release wrap validator passes. Post-publish GHCR, deployment refresh, and development-reopen evidence are recorded separately in `docs/RELEASE_WRAP_0.21.2.md` after publication.

## Upgrade Note

Deploy `v0.21.2` over `v0.21.1` when the release is published if you use the admin setup/maintenance UI with saved credentials. The patch does not intentionally change Storage Fabric routing, history retention behavior, Quantastor visibility, or public demo data.
