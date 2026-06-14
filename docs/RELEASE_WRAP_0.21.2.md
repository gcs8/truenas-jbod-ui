# Release Wrap - v0.21.2

Date: `2026-06-13`

## Scope

`v0.21.2` is a narrow SemVer patch on top of the published `v0.21.1` release. It includes the post-`v0.21.1` repository hygiene now on `main` plus the admin-state credential redaction hardening from PR #21.

This release-prep packet intentionally excludes the open Dependabot PRs #17-#20 and the broader `v0.22.0-dev` performance guardrail work.

Current pre-tag state: PR #22 was merged to `main` at commit `cd86f81`; release-candidate version metadata is `0.21.2`.

Validated against `docs/RELEASE_CHECKLIST.md`.

Pre-tag automated gates are now complete. Tag/publish only after the strict pre-tag validator passes and the release action is intentionally executed.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | release-prep PR #22 merged to `main` commit `cd86f81`; latest published release remains `v0.21.1`; target version metadata moved from the prior dev lane to `0.21.2`; scope is admin-state redaction hardening plus CI/repo hygiene, with Dependabot PRs #17-#20 and `v0.22.0-dev` performance work excluded | Pass |  |
| Python unit and syntax gates | yes | `.venv/bin/python -m compileall -q app admin_service history_service scripts tests` passed; `.venv/bin/python -m unittest tests.test_admin_service tests.test_account_bootstrap tests.test_system_backup -q` passed `103` tests; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `516` tests with `4` skipped | Pass |  |
| JavaScript syntax gates | yes | `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` passed; `git diff --check` passed | Pass |  |
| Docker build and health gates | yes | built local release-candidate image `truenas-jbod-ui:v0.21.2-local`; isolated UI/history/admin stack ran on `19080/19081/19082`; `/livez` for UI/history/admin reported `version: 0.21.2`; `/healthz` for all three services reported `status: ok`; seeded runtime exposed 11 configured systems | Pass |  |
| Optional-sidecar runtime matrix | yes | isolated matrix passed: UI-only `/livez`/`/healthz` plus two Playwright smoke tests passed; UI+history reported history `configured: true`, `available: true`, and the history-sidecar action smoke passed; UI+admin health passed and `qa/admin-operations.spec.js` passed 5 tests; full UI/history/admin stack restored with all service health checks ok | Pass |  |
| Full Playwright/browser gates | yes | Local seeded stack: `PYTHON=.venv/bin/python PLAYWRIGHT_BASE_URL=http://127.0.0.1:19080 PLAYWRIGHT_ADMIN_BASE_URL=http://127.0.0.1:19082 npx playwright test` passed with 25 passed, 2 skipped. Restored Linux QA stack: `PYTHON=.venv/bin/python PLAYWRIGHT_BASE_URL=http://10.13.37.138:18180 PLAYWRIGHT_ADMIN_BASE_URL=http://10.13.37.138:18182 npx playwright test` passed with 26 passed, 1 skipped | Pass |  |
| Feature-specific live API/UI gates | yes | local admin-state redaction probe checked configured secret values from the seeded runtime against `/api/admin/state` and `/`; result: 5 secret values checked, 0 leaks, 55 configured markers, 11 systems in state, and no raw script-breakout sequence in the admin HTML. Restored Linux QA admin container repeated the same live redaction check with 5 secret values checked, 0 leaks, 55 configured markers, 11 systems in state, and no script-breakout sequence | Pass |  |
| Local release perf harnesses | yes | `scripts/run_perf_harness.py --base-url http://127.0.0.1:19080 --iterations 3 --label release-candidate-v0.21.2-local` passed and wrote `data/perf/latest.md`; cached health avg 2.1 ms, cached inventory avg 4.3 ms, history status avg 5.4 ms, snapshot estimate avg 151.1 ms, forced inventory avg 17.6 s. `scripts/run_history_perf_harness.py --base-url http://127.0.0.1:19081 --iterations 3 --label release-candidate-v0.21.2-local-history` passed and wrote `data/history-perf/latest.md`; estimated overview avg 4.2 ms, dashboard avg 5.1 ms, DB size 999.2 MiB, background failures 0 | Pass |  |
| Linux QA restore gate | yes | fresh disposable QA stack was created on `codex-dev-test-target` / `10.13.37.138` without disturbing the existing `0.21.1` source or QA stacks: runtime `/docker-local/truenas-jbod-ui-qa-release-0.21.2-20260613T232658Z/repo`, commit `cd86f81`, compose project `truenas_jbod_ui_qa_release_0212`, ports `18180/18181/18182`, containers `truenas-jbod-ui-qa-release-0212`, `truenas-jbod-history-qa-release-0212`, `truenas-jbod-admin-qa-release-0212`; source history DB copied via SQLite backup (`1,081,786,368` bytes, SHA-256 `845f5107810e63ac1f6c6417ffc355be5604453e0ab469c832bb005a5a927faa`); UI/history/admin `/livez` reported `version: 0.21.2` and `/healthz` reported `status: ok`; exact source-vs-QA provenance matched after browser/perf/export: 11 systems, platform counts core=1/esxi=3/ipmi=1/linux=4/quantastor=1/scale=1, 60 slots, 2 storage views, 347 tracked slots, 20,305 history events, 1,418,806 metric samples, 23 scopes, QA collector idle with `last_error: null` | Pass |  |
| Restored Linux QA perf harnesses | yes | after `http://10.13.37.138:18181/healthz` showed `collection_running=false`, restored perf passed: `scripts/run_perf_harness.py --base-url http://10.13.37.138:18180 --iterations 3 --format markdown --label release-candidate-v0.21.2-linux-qa-fullsource` wrote `data/perf/latest.md` with cached health avg 3.2 ms, history status avg 8.8 ms, cached inventory avg 37.6 ms, storage views avg 29.0 ms, snapshot estimate avg 2,028.2 ms, forced inventory avg 28.5 s; `scripts/run_history_perf_harness.py --base-url http://10.13.37.138:18181 --iterations 3 --format markdown --label release-candidate-v0.21.2-linux-qa-fullsource-history` wrote `data/history-perf/latest.md` with sidecar health avg 1.6 ms, estimated overview avg 3.5 ms, dashboard avg 12.4 ms, DB size 1.0 GiB, background failures 0 | Pass |  |
| Snapshot/export/offline artifact gate | yes | checked-in public demo publishability passed via `.venv/bin/python scripts/check_public_demo_artifact.py public-demo`; local release-candidate export estimate returned HTML size 1,821,197 bytes and ZIP download was 903,500 bytes with SHA-256 `cb35a937620cac23b4e2188398a850d186af5258b9f92b29e8b10d22db85fee5`; local offline HTML opened in Chromium with 60 slots, 11 system options, and 0 console/page errors. Restored Linux QA full-source export against `http://10.13.37.138:18180` produced `artifacts/private-v0.21.2/linux-qa-fullsource-snapshot-export/linux-qa-fullsource-snapshot-export-fullscope-20260613T2338Z.zip`, 1,123,096 bytes, SHA-256 `0c2eac4d46b4368c73a121ee91a8893e02e24faebb7974f7f8e845ccb63e6d49`, HTML size 9,269,662 bytes; offline Chromium smoke found 60 slots, 11 system options, 4 enclosure options, storage-view markers present, no horizontal overflow, and 0 console/page issues | Pass |  |
| Docs/wiki/public-demo gate | yes | `CHANGELOG.md`, `docs/RELEASE_NOTES_0.21.2.md`, this wrap, `docs/ROADMAP.md`, and repo-local `wiki/Home.md` were updated for `v0.21.2`; `.venv/bin/python scripts/check_public_demo_artifact.py public-demo` reported publishable `public-demo/index.html` at 7,178,450 bytes; active-current-version scan found no stale `0.21.2-dev` release metadata outside historical release-wrap wording | Pass |  |
| GHCR publish verification | yes | post-publish gate: blocked until tag and GitHub release publish trigger the GHCR workflow and digest convergence is recorded | Blocked |  |
| Deployment refresh/sniff tests | yes | post-publish gate: blocked until GHCR image is available and local/Linux/production deployment refresh and sniff tests are recorded | Blocked |  |
| Post-release reopen | yes | post-publish gate: blocked until `0.21.2` ships and `main` is reopened to the agreed next lane, currently `0.22.0-dev` | Blocked |  |

## Pre-Tag Validator Status

Expected pre-tag validator behavior:

- `.venv/bin/python scripts/validate_release_wrap.py 0.21.2 --phase pre-tag --allow-blocked` should pass, proving the evidence table shape is complete.
- `.venv/bin/python scripts/validate_release_wrap.py 0.21.2 --phase pre-tag` should pass now that every pre-publish gate is `Pass`; only inherently post-publish rows remain blocked.

## Remaining Pre-Tag Work

1. Re-run strict pre-tag validation and only tag after every pre-tag row is `Pass` or justified `N/A`.

## Publish Result

Blocked in this prep packet. No `v0.21.2` tag, GitHub Release, GHCR digest, deployment refresh, or post-release reopen has been produced yet.
