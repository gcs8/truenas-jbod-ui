# Release Wrap - v0.21.0

Date: `2026-06-11`

## Scope

`v0.21.0` is the maintenance and confidence pitstop after the Storage Fabric
expansion. It intentionally avoids a broad feature catch-all and preserves the
operator contract for slot identity, LED/locator boundaries, physical-disk
situational awareness, and honest source-labeled visibility across CORE, SCALE,
Quantastor, Linux, ESXi, and BMC/IPMI paths.

Included release-prep changes:

- app/package metadata bump from `0.21.0-dev` to `0.21.0`
- `CHANGELOG.md` `v0.21.0` section
- `docs/RELEASE_NOTES_0.21.0.md`
- this initial `docs/RELEASE_WRAP_0.21.0.md`
- PR #7 SSH fanout and Quantastor HA targeting follow-up, merged to `main` at
  commit `6fe534b`

Included v0.21 work already merged to `main` before this prep packet:

- PR #1: confidence pitstop rails, CI, public-demo strategy, and backup import hardening
- PR #2: Storage Fabric selector seam
- PR #3: Storage Fabric builder registry wrapper
- PR #4: Storage Fabric platform route registry
- PR #7: reduced SSH fanout for inventory, SMART, and Quantastor enrichment;
  explicit HA node SSH targeting; SSH startup backoff/redaction; CI
  browser-smoke hardening

This wrap is intentionally an initial release-prep wrap. It is not a tag-ready
wrap until every required pre-publish `Blocked` row below is replaced with
recorded `Pass` evidence and `scripts/validate_release_wrap.py 0.21.0 --phase
pre-tag` succeeds without `--allow-blocked`.

## Checklist Evidence

Validated against `docs/RELEASE_CHECKLIST.md`.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | final release branch `codex/v0.21.0-release-final-20260611` from `main` merge commit `6fe534b`; target version `0.21.0`; includes release-prep PRs #1-#5 plus SSH fanout PR #7; gcs8 explicitly approved merge/release gate work in the 2026-06-11 Hermes thread | Pass |  |
| Python unit and syntax gates | yes | release-final branch `codex/v0.21.0-release-final-20260611`: `.venv/bin/python -m compileall -q app admin_service history_service scripts tests` passed; `.venv/bin/python -m unittest tests.test_release_status -v` passed `4`; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `503` tests with `4` skipped | Pass |  |
| JavaScript syntax gates | yes | release-final branch: `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` passed | Pass |  |
| Docker build and health gates | yes | blocked for release-prep: release-candidate Docker image and UI/history/admin health gates have not been run for `0.21.0` yet | Blocked |  |
| Optional-sidecar runtime matrix | yes | blocked for release-prep: UI-only, UI plus history, UI plus admin, and full-stack optional sidecar matrix still needs `0.21.0` runtime evidence | Blocked |  |
| Full Playwright/browser gates | yes | blocked for release-prep: full browser suite has not yet been run against a `0.21.0` release-candidate stack | Blocked |  |
| Feature-specific live API/UI gates | yes | blocked for release-prep: Storage Fabric CORE, SCALE/Linux SES, Quantastor, ESXi, BMC/IPMI, unsupported or weak-evidence copy, and browser-console checks still need release-candidate evidence | Blocked |  |
| Local release perf harnesses | yes | blocked for release-prep: local main and history perf harnesses still need `0.21.0` release-candidate labels and artifact paths | Blocked |  |
| Linux QA restore gate | yes | blocked for release-prep: isolated Linux QA restore stack on non-default ports still needs export/import, restored counts, and health evidence | Blocked |  |
| Restored Linux QA perf harnesses | yes | blocked for release-prep: restored Linux QA main and history perf harnesses still need `0.21.0` labels and artifact paths | Blocked |  |
| Snapshot/export/offline artifact gate | yes | blocked for release-prep: restored-stack snapshot estimate, forced ZIP or equivalent artifact download, and offline browser smoke still need evidence | Blocked |  |
| Docs/wiki/public-demo gate | yes | release-final branch docs updated for PR #7; checked-in public demo artifact passed `.venv/bin/python scripts/check_public_demo_artifact.py public-demo` with artifact size `7178450` bytes; final gate remains blocked until public-demo browser smoke, public-demo freshness/acceptance, and wiki/docs stale-wording assessment are recorded | Blocked |  |
| GHCR publish verification | yes | post-publish gate: blocked until tag and GitHub release publish trigger the GHCR workflow and digest convergence is recorded | Blocked |  |
| Deployment refresh/sniff tests | yes | post-publish gate: blocked until GHCR image is available and local, Linux, and production deployment sniff tests are recorded | Blocked |  |
| Post-release reopen | yes | post-publish gate: blocked until `0.21.0` ships and the next development version/branch is reopened | Blocked |  |

## Prep Validation Notes

The initial wrap should pass the shape check only with blocked gates allowed:

- `python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag --allow-blocked`

Before tagging, it must pass without blocked pre-publish rows:

- `python scripts/validate_release_wrap.py 0.21.0 --phase pre-tag`

After GHCR, deployment sniff tests, and post-release reopen are recorded, it must
pass the final validator:

- `python scripts/validate_release_wrap.py 0.21.0`

## Publish Result

- Release commit: `TBD`
- Tag: `v0.21.0` when cut
- GitHub release: `TBD`
- GHCR digest: `TBD`
- Public demo workflow: `TBD if artifact changes during final release work`
- External wiki sync: `TBD after docs/wiki gate`
- Post-release development branch: `TBD`

## Notes

- No release tag should be pushed until strict pre-tag validation passes without
  `--allow-blocked`.
- Runtime validation should use an isolated QA Docker stack on the Linux Codex
  dev target with non-default ports, unless gcs8 explicitly requests updating
  the long-running review stack with the complete current change set.
- Do not leave the admin sidecar public-facing or long-running during validation.
