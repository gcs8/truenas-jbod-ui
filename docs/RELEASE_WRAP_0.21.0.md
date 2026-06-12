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
| Feature-specific live API/UI gates | yes | reran against the operator-approved real-data source clone on `10.13.37.138:18080`: `/api/inventory` showed `6` systems (`archive-core`, `offsite-scale`, `gpu-server`, `unvr`, `unvr-pro`, `qsosn-ha`), `60` slots, CORE `1`, SCALE `1`, Linux `3`, Quantastor `1`; `/api/storage-views` covered `11` storage views across all `6` systems; cached `/api/sas-fabric` covered `6` available fabric systems with `686` links and `7` warnings; forced provenance comparison against the source `10.13.37.138:8080` matched system IDs, platform counts, slot count, storage counts, and forced SAS counts (`689` links, `17` warnings); representative `/` and `/sas-fabric` browser pages for CORE/Linux/Quantastor/SCALE had no page-level horizontal overflow and no browser error/warning console messages | Pass |  |
| Local release perf harnesses | yes | final rebuilt stack perf rerun: `scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate` wrote `data/perf/latest.md` (`inventory_cached` avg `3.8 ms`, `inventory_force` avg `21569.8 ms`); `scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label release-candidate-history` wrote `data/history-perf/latest.md` (`overview_estimated` avg `3.7 ms`, DB `989.4 MiB`, `1,362,917` metric samples) | Pass |  |
| Linux QA restore gate | yes | operator identified `http://10.13.37.138:8080/` as the real-data source. Exported default backup from `:8082` to ignored artifact `artifacts/private-v0.21.0/real-data-source-10.13.37.138-8080/real-data-source-10.13.37.138-8080-20260612T041856Z.tar.zst` (`49014931` bytes, SHA-256 `3cf6472393366e27ea9206e85e8c09627396e49954bf64b6cd6c6a17f840b3ce`) plus encrypted sensitive transport backup `real-data-source-10.13.37.138-8080-sensitive-transport-20260612T042947Z.7z` (`6991` bytes, SHA-256 `ca24d3f927a240a62d8603fb31de0ddef481467968978d78efc47f8fc521693d`); restored both into fresh disposable runtime `/docker-local/truenas-jbod-ui-qa-realdata-0.21.0-20260612T042014Z/repo` at commit `0bd14dc` with unique containers and ports `18080/18081/18082`; UI/history/admin `/livez` and `/healthz` returned `status=ok`, version `0.21.0`; automated real-data provenance now matches the source, and Playwright against `18080/18082` passed `24` with `3` expected skips. Still blocked pending gcs8 visual acceptance of the restored real-data candidate at `http://10.13.37.138:18080/`. | Blocked |  |
| Restored Linux QA perf harnesses | yes | after real-data restore and `:18081/healthz` `collection_running=false`, reran serial perf harnesses: `scripts/run_perf_harness.py --base-url http://10.13.37.138:18080 --iterations 3 --format markdown --label release-candidate-linux-qa-realdata` wrote `data/perf/latest.md` (`inventory_cached` avg `9.5 ms`, `inventory_force` avg `12277.7 ms`, `snapshot_export_estimate` avg `275.6 ms`); `scripts/run_history_perf_harness.py --base-url http://10.13.37.138:18081 --iterations 3 --format markdown --label release-candidate-history-linux-qa-realdata` wrote `data/history-perf/latest.md` (`overview_estimated` avg `4.7 ms`, DB `1.4 GiB`, `1,932,445` metric samples, `collection_running=false`) | Pass |  |
| Snapshot/export/offline artifact gate | yes | local mechanics smoke remains as previously recorded; Linux QA real-data stack repeated snapshot estimate/download/offline smoke against `http://10.13.37.138:18080`: forced ZIP artifact `artifacts/private-v0.21.0/linux-qa-realdata-snapshot-export/linux-qa-realdata-snapshot-export-force-zip.zip` was `1525602` bytes with SHA-256 `d5b7404f4b3511d7d35978603a3527464803cc0659f3a4e547ec56a7e648939c`; estimate reported HTML `16338318` bytes and ZIP `1525571` bytes; extracted offline HTML opened in Playwright with `6` systems, `60` tiles, a snapshot banner, no horizontal overflow, and no browser error/warning console messages | Pass |  |
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
