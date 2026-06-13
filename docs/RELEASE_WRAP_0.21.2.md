# Release Wrap - v0.21.2

Date: `2026-06-13`

## Scope

`v0.21.2` is a narrow SemVer patch on top of the published `v0.21.1` release. It includes the post-`v0.21.1` repository hygiene now on `main` plus the admin-state credential redaction hardening from PR #21.

This release-prep packet intentionally excludes the open Dependabot PRs #17-#20 and the broader `v0.22.0-dev` performance guardrail work.

Current release-prep branch: `release/v0.21.2-prep` from `main` commit `9a2b457` (`fix(admin): redact saved state secrets (#21)`).

Validated against `docs/RELEASE_CHECKLIST.md`.

**Do not tag or publish from this prep state.** The checklist shape is ready for prep review, but multiple pre-tag release gates remain `Blocked` until real runtime, restored-QA, browser, perf, and artifact evidence is recorded.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | release-prep branch `release/v0.21.2-prep` from `main` `9a2b457`; latest published release remains `v0.21.1`; target version metadata moved from `0.21.2-dev` to `0.21.2`; scope is admin-state redaction hardening plus CI/repo hygiene, with Dependabot PRs #17-#20 and `v0.22.0-dev` performance work excluded | Pass |  |
| Python unit and syntax gates | yes | `.venv/bin/python -m compileall -q app admin_service history_service scripts tests` passed; `.venv/bin/python -m unittest tests.test_admin_service tests.test_account_bootstrap tests.test_system_backup -q` passed `103` tests; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `516` tests with `4` skipped | Pass |  |
| JavaScript syntax gates | yes | `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` passed; `git diff --check` passed | Pass |  |
| Docker build and health gates | yes | release-candidate Docker image/build and UI `/livez`/`/healthz` sniff have not been run for `0.21.2` yet | Blocked |  |
| Optional-sidecar runtime matrix | yes | UI-only, UI+history, UI+admin, and full UI/history/admin sidecar runtime matrix has not been rerun for `0.21.2` yet | Blocked |  |
| Full Playwright/browser gates | yes | browser smoke suite has not been run against a `0.21.2` release-candidate stack yet | Blocked |  |
| Feature-specific live API/UI gates | yes | admin-state redaction has source-level regressions, but live admin API/UI checks against a running `0.21.2` release-candidate stack are still pending | Blocked |  |
| Local release perf harnesses | yes | local release performance harnesses have not been run against a `0.21.2` release-candidate stack yet | Blocked |  |
| Linux QA restore gate | yes | disposable Linux QA restore from the approved full-data source has not been built/restored/sniffed for `0.21.2` yet | Blocked |  |
| Restored Linux QA perf harnesses | yes | restored Linux QA main and history perf harnesses have not been run for `0.21.2` yet | Blocked |  |
| Snapshot/export/offline artifact gate | yes | checked-in public demo publishability passed via `.venv/bin/python scripts/check_public_demo_artifact.py public-demo`, but release-candidate snapshot export/download/offline browser smoke is still pending | Blocked |  |
| Docs/wiki/public-demo gate | yes | `CHANGELOG.md`, `docs/RELEASE_NOTES_0.21.2.md`, this wrap, `docs/ROADMAP.md`, and repo-local `wiki/Home.md` were updated for the prep packet; public demo artifact publishability passed, but final stale-version scan, public-demo freshness decision, and external wiki/publication evidence are still pending | Blocked |  |
| GHCR publish verification | yes | post-publish gate: blocked until tag and GitHub release publish trigger the GHCR workflow and digest convergence is recorded | Blocked |  |
| Deployment refresh/sniff tests | yes | post-publish gate: blocked until GHCR image is available and local/Linux/production deployment refresh and sniff tests are recorded | Blocked |  |
| Post-release reopen | yes | post-publish gate: blocked until `0.21.2` ships and `main` is reopened to the agreed next lane, currently `0.22.0-dev` | Blocked |  |

## Prep Validator Status

Expected prep-state validator behavior:

- `.venv/bin/python scripts/validate_release_wrap.py 0.21.2 --phase pre-tag --allow-blocked` should pass, proving the evidence table shape is complete.
- `.venv/bin/python scripts/validate_release_wrap.py 0.21.2 --phase pre-tag` should fail while the non-post-publish runtime/QA/browser/perf/artifact rows remain blocked.

## Remaining Pre-Tag Work

1. Build the `0.21.2` release-candidate Docker stack and record UI/history/admin health.
2. Run the optional-sidecar runtime matrix.
3. Run the browser smoke suite against the release-candidate stack.
4. Run feature-specific live admin API/UI redaction/preserve checks.
5. Run local release perf harnesses.
6. Seed and validate the disposable Linux QA restore stack from the approved full-data source, then run restored browser/perf/export gates.
7. Complete docs/wiki/public-demo freshness checks and external wiki/publication evidence as applicable.
8. Re-run strict pre-tag validation and only tag after every pre-tag row is `Pass` or justified `N/A`.

## Publish Result

Blocked in this prep packet. No `v0.21.2` tag, GitHub Release, GHCR digest, deployment refresh, or post-release reopen has been produced yet.
