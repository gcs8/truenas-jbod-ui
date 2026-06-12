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
- local release-gate hardening found during restored-data validation: SMART
  prefetch abort/fallback console noise demoted out of the operator error
  console, restored-data browser timeout handling, heat-map restored-system
  coverage, and current-version roadmap/wiki wording
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
| Scope and branch | yes | final release branch `codex/v0.21.0-release-final-20260611` from `main` merge commit `6fe534b`; target version `0.21.0`; includes release-prep PRs #1-#5, SSH fanout PR #7, and local release-gate hardening found during restored-data validation; gcs8 explicitly approved merge/release gate work in the 2026-06-11 Hermes thread | Pass |  |
| Python unit and syntax gates | yes | rebuilt local release branch: `.venv/bin/python -m compileall -q app admin_service history_service scripts tests` passed; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `503` tests with `4` skipped; earlier targeted `tests.test_release_status -v` passed `4` | Pass |  |
| JavaScript syntax gates | yes | release-final branch: `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, `qa/public-demo.spec.js`, and `qa/ui-switching.spec.js` passed; `git diff --check` passed | Pass |  |
| Docker build and health gates | yes | rebuilt release-candidate Docker images from the current branch with `docker compose -f docker-compose.dev.yml --profile history --profile admin up -d --build`; UI/history/admin `/livez` and `/healthz` on `8080/8081/8082` returned `status=ok`, UI `/livez` version `0.21.0` | Pass |  |
| Optional-sidecar runtime matrix | yes | final rebuilt image matrix passed: UI-only `:8080/livez`/`:8080/healthz` plus two browser smoke tests; UI+history `:8081/livez`/`:8081/healthz`, `/api/history/status configured=true available=true`, and two history UI tests; UI+admin `:8082/livez`/`:8082/healthz` plus six admin/history-unavailable tests; full stack UI/history/admin `/livez`/`/healthz` all `status=ok` | Pass |  |
| Full Playwright/browser gates | yes | restored local full-stack run with `PYTHON=.venv/bin/python PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080 PLAYWRIGHT_ADMIN_BASE_URL=http://127.0.0.1:8082 npx playwright test` passed `26`, skipped `1` intentionally perf-only auto-refresh test | Pass |  |
| Feature-specific live API/UI gates | yes | local restored API/UI checks covered CORE, SCALE, GPU/Linux, UniFi Linux, Quantastor HA, ESXi, BMC/IPMI: `/api/inventory`, `/api/storage-views`, `/api/sas-fabric`, dedicated `/sas-fabric` browser pages, first-click selection, no horizontal overflow, admin ESXi/runtime surfaces, and no browser error/warning console messages on release-facing paths; snapshot export estimate/download also covered Auto-to-ZIP packaging | Pass |  |
| Local release perf harnesses | yes | final rebuilt stack perf rerun: `scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate` wrote `data/perf/latest.md` (`inventory_cached` avg `3.8 ms`, `inventory_force` avg `21569.8 ms`); `scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label release-candidate-history` wrote `data/history-perf/latest.md` (`overview_estimated` avg `3.7 ms`, DB `989.4 MiB`, `1,362,917` metric samples) | Pass |  |
| Linux QA restore gate | yes | disposable QA stack built on `10.13.37.138` from commit `535c61a` in `/docker-local/truenas-jbod-ui-qa-0.21.0-20260612T034656Z/repo` with unique QA container names and ports `18080/18081/18082`; restored ignored Windows bundle `artifacts/private-v0.21.0/windows-restore-default.tar.zst` (`34089681` bytes, SHA-256 `0a6980f2e6da37fbe8763dd5a3cce744f234fae89d35bd4620dfffb6826aeb25`) through the disposable admin API; UI/history/admin `/livez` and `/healthz` all returned `status=ok` with version `0.21.0`; restored inventory showed `11` systems across CORE `1`, SCALE `1`, Linux `4`, Quantastor `1`, ESXi `3`, and BMC/IPMI `1`; remote Playwright with `PLAYWRIGHT_BASE_URL=http://10.13.37.138:18080` and `PLAYWRIGHT_ADMIN_BASE_URL=http://10.13.37.138:18082` passed `26` with `1` intentional skip; Linux QA API/UI probe covered `/api/inventory`, `/api/storage-views`, `/api/sas-fabric`, and representative `/` plus `/sas-fabric` browser pages for CORE, SCALE, Linux, Quantastor, ESXi, and BMC/IPMI with `19` storage views, `10` SAS/Storage Fabric available systems, `292` fabric links, no page-level horizontal overflow, and no browser error/warning console messages | Pass |  |
| Restored Linux QA perf harnesses | yes | initial perf attempt overlapped a restored history background collection, so the collector was polled until `collection_running=false` and both harnesses were rerun serially; final main run `scripts/run_perf_harness.py --base-url http://10.13.37.138:18080 --iterations 3 --format markdown --label release-candidate-linux-qa-restore` wrote `data/perf/latest.md` (`inventory_cached` avg `4.5 ms`, `inventory_force` avg `33689.6 ms`, `snapshot_export_estimate` avg `289.3 ms`); final history run `scripts/run_history_perf_harness.py --base-url http://10.13.37.138:18081 --iterations 3 --format markdown --label release-candidate-history-linux-qa-restore` wrote `data/history-perf/latest.md` (`overview_estimated` avg `7.5 ms`, DB `989.4 MiB`, `1,362,928` metric samples, `collection_running=false`) | Pass |  |
| Snapshot/export/offline artifact gate | yes | local restored stack `/api/export/enclosure-snapshot/estimate` with storage views, live enclosures, 168h history, and redaction returned Auto→HTML allowed (`3.5 MiB`, ZIP `948.1 KiB`); forced ZIP download wrote ignored artifact `artifacts/private-v0.21.0/local-snapshot-export-force-zip.zip` (`970822` bytes, SHA-256 `3067e0e04d91a6b729c88accf4f6658f38bea8ccf2ec925a3b09441ba8f5a8be`); extracted offline HTML opened in Playwright with `11` systems, `60` tiles, and no console messages. Linux QA restored stack repeated the export/download/offline smoke against `http://10.13.37.138:18080`: forced ZIP artifact `artifacts/private-v0.21.0/linux-qa-snapshot-export/linux-qa-snapshot-export-force-zip.zip` was `970296` bytes with SHA-256 `645351495b54142c2fe2333faa07066168420d0030cf7532570d3e971e0a4a8a`, and extracted offline HTML opened in Playwright with `11` systems, `60` tiles, a snapshot banner, and no browser error/warning console messages | Pass |  |
| Docs/wiki/public-demo gate | yes | `CHANGELOG.md`, `docs/RELEASE_NOTES_0.21.0.md`, `docs/ROADMAP.md`, and `wiki/Home.md` updated for PR #7 plus local-gate hardening/current-version wording; stale current-version scan found no `0.21.0-dev`/old-current wording in README, roadmap, public-demo README, or wiki home; `.venv/bin/python scripts/check_public_demo_artifact.py public-demo` passed (`7178450` bytes); full Playwright run included `qa/public-demo.spec.js` pass | Pass |  |
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
