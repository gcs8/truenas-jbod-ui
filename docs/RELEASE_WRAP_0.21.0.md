# Release Wrap - v0.21.0

Date: `2026-05-22`

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

Included v0.21 work already merged to `main` before this prep packet:

- PR #1: confidence pitstop rails, CI, public-demo strategy, and backup import hardening
- PR #2: Storage Fabric selector seam
- PR #3: Storage Fabric builder registry wrapper
- PR #4: Storage Fabric platform route registry

This wrap is intentionally an initial release-prep wrap. It is not a tag-ready
wrap until every required pre-publish `Blocked` row below is replaced with
recorded `Pass` evidence and `scripts/validate_release_wrap.py 0.21.0 --phase
pre-tag` succeeds without `--allow-blocked`.

## Checklist Evidence

Validated against `docs/RELEASE_CHECKLIST.md`.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | release-prep branch `codex/v0.21.0-release-prep-2026-05-22` from protected `main` commit `09e2a22`; target version `0.21.0`; scope limited to version metadata and release docs | Pass |  |
| Python unit and syntax gates | yes | release-prep branch: `python -m compileall -q app admin_service history_service scripts tests` passed; `python -m unittest tests.test_release_status -v` passed `4`; `python -m unittest discover -s tests -p "test_*.py" -q` passed `478` tests with `4` skipped | Pass |  |
| JavaScript syntax gates | yes | release-prep branch: `npm ci --ignore-scripts` passed; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` passed | Pass |  |
| Docker build and health gates | yes | blocked for release-prep: release-candidate Docker image and UI/history/admin health gates have not been run for `0.21.0` yet | Blocked |  |
| Optional-sidecar runtime matrix | yes | blocked for release-prep: UI-only, UI plus history, UI plus admin, and full-stack optional sidecar matrix still needs `0.21.0` runtime evidence | Blocked |  |
| Full Playwright/browser gates | yes | blocked for release-prep: full browser suite has not yet been run against a `0.21.0` release-candidate stack | Blocked |  |
| Feature-specific live API/UI gates | yes | blocked for release-prep: Storage Fabric CORE, SCALE/Linux SES, Quantastor, ESXi, BMC/IPMI, unsupported or weak-evidence copy, and browser-console checks still need release-candidate evidence | Blocked |  |
| Local release perf harnesses | yes | blocked for release-prep: local main and history perf harnesses still need `0.21.0` release-candidate labels and artifact paths | Blocked |  |
| Linux QA restore gate | yes | blocked for release-prep: isolated Linux QA restore stack on non-default ports still needs export/import, restored counts, and health evidence | Blocked |  |
| Restored Linux QA perf harnesses | yes | blocked for release-prep: restored Linux QA main and history perf harnesses still need `0.21.0` labels and artifact paths | Blocked |  |
| Snapshot/export/offline artifact gate | yes | blocked for release-prep: restored-stack snapshot estimate, forced ZIP or equivalent artifact download, and offline browser smoke still need evidence | Blocked |  |
| Docs/wiki/public-demo gate | yes | release-prep docs created; checked-in public demo artifact passed `python scripts/check_public_demo_artifact.py public-demo`; `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js` passed `1`; final gate remains blocked until the public demo artifact is refreshed or explicitly accepted for `v0.21.0` and stale roadmap/wiki wording is assessed | Blocked |  |
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

- No release tag should be pushed from this initial prep state.
- Runtime validation should use an isolated QA Docker stack on the Linux Codex
  dev target with non-default ports, unless gcs8 explicitly requests updating
  the long-running review stack with the complete current change set.
- Do not leave the admin sidecar public-facing or long-running during validation.
