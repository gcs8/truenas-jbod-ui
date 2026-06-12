# Release Wrap - v0.21.0

Date: `2026-06-12`

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

This wrap is pre-tag ready once `scripts/validate_release_wrap.py 0.21.0 --phase
pre-tag` succeeds without `--allow-blocked`. Post-publish gates remain blocked
until tag/release/GHCR/deployment/reopen evidence exists.

## Checklist Evidence

Validated against `docs/RELEASE_CHECKLIST.md`.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | final release branch `codex/v0.21.0-release-final-20260611` from `main` merge commit `6fe534b`; target version `0.21.0`; includes release-prep PRs #1-#5, SSH fanout PR #7, and local release-gate hardening found during restored-data validation; gcs8 explicitly approved merge/release gate work in the 2026-06-11 Hermes thread | Pass |  |
| Python unit and syntax gates | yes | rebuilt local release branch: `.venv/bin/python -m compileall -q app admin_service history_service scripts tests` passed; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `503` tests with `4` skipped; earlier targeted `tests.test_release_status -v` passed `4` | Pass |  |
| JavaScript syntax gates | yes | release-final branch: `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, `qa/public-demo.spec.js`, and `qa/ui-switching.spec.js` passed; `git diff --check` passed | Pass |  |
| Docker build and health gates | yes | rebuilt release-candidate Docker images from the current branch with `docker compose -f docker-compose.dev.yml --profile history --profile admin up -d --build`; UI/history/admin `/livez` and `/healthz` on `8080/8081/8082` returned `status=ok`, UI `/livez` version `0.21.0` | Pass |  |
| Optional-sidecar runtime matrix | yes | final rebuilt image matrix passed: UI-only `:8080/livez`/`:8080/healthz` plus two browser smoke tests; UI+history `:8081/livez`/`:8081/healthz`, `/api/history/status configured=true available=true`, and two history UI tests; UI+admin `:8082/livez`/`:8082/healthz` plus six admin/history-unavailable tests; full stack UI/history/admin `/livez`/`/healthz` all `status=ok` | Pass |  |
| Full Playwright/browser gates | yes | corrected full-data Linux QA run against `10.13.37.138:18080/18082` with `PYTHON=.venv/bin/python PLAYWRIGHT_BASE_URL=http://10.13.37.138:18080 PLAYWRIGHT_ADMIN_BASE_URL=http://10.13.37.138:18082 npx playwright test` passed `27`/`27` on commit `f7d3829`; this supersedes earlier 6-system restored-data Playwright evidence | Pass |  |
| Feature-specific live API/UI gates | yes | reran against corrected full-data Linux QA at `10.13.37.138:18080`: `/api/inventory` showed `11` systems (`archive-core`, `offsite-scale`, `gpu-server`, `unvr`, `unvr-pro`, `qsosn-ha`, `demo-builder-lab`, `cryostorage-esxi`, `ipmi-ft-1`, `esxi-ft-node-2`, `esxi-ft-node-3`), `60` slots, CORE `1`, SCALE `1`, Linux `4`, Quantastor `1`, ESXi `3`, IPMI `1`; `/api/storage-views` showed `2` storage views; cached `/api/sas-fabric` covered `11` available fabric systems with `799` links and `13` warnings; forced `/api/sas-fabric` covered `11` available fabric systems with `799` links and `22` warnings; representative `/` and `/sas-fabric` browser pages for CORE, SCALE, Linux, Quantastor, ESXi, and IPMI/BMC had no page-level horizontal overflow and no browser error/warning console messages | Pass |  |
| Local release perf harnesses | yes | final rebuilt stack perf rerun: `scripts/run_perf_harness.py --base-url http://127.0.0.1:8080 --iterations 3 --format markdown --label release-candidate` wrote `data/perf/latest.md` (`inventory_cached` avg `3.8 ms`, `inventory_force` avg `21569.8 ms`); `scripts/run_history_perf_harness.py --base-url http://127.0.0.1:8081 --iterations 3 --format markdown --label release-candidate-history` wrote `data/history-perf/latest.md` (`overview_estimated` avg `3.7 ms`, DB `989.4 MiB`, `1,362,917` metric samples) | Pass |  |
| Linux QA restore gate | yes | `.67` was confirmed as the fuller source of truth, frozen, and migrated into the long-running `.138` source without rerunning risky `stop_services=true` admin export. Corrected source `10.13.37.138:8080/8081/8082` and disposable QA `10.13.37.138:18080/18081/18082` both returned healthy `/livez`/`/healthz`; QA runtime `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo`, compose project `truenas_jbod_ui_qa_release_0210`, unique containers and ports `18080/18081/18082`. QA provenance matched the frozen 11-system baseline: system/platform set `core=1`, `scale=1`, `linux=4`, `quantastor=1`, `esxi=3`, `ipmi=1`; `60` slots; `2` storage views; `23` history scopes. Follow-up hotfix refreshes were deployed in-place for history event-noise, Quantastor SSH warning collapse, and Quantastor `Visible On` scoping. gcs8 visual review on `http://10.13.37.138:18080/` found the final candidate acceptable after the junk same-day history entries were cleaned. QA history cleanup backed up the DB to `/docker-local/truenas-jbod-ui-qa-release-0.21.0-20260612T111913Z/repo/history/manual-cleanup-backups/history-pre-noise-cleanup-20260612T164034Z.sqlite3`, deleted `1,905` same-day noisy `slot_identity_changed`/`slot_topology_changed` rows (`1,341` identity, `564` topology), and left exact QA counts at `347` tracked slots, `17,841` events, `1,372,400` metric samples, `23` scopes; one fast refresh after cleanup completed with `last_error=null`, `last_scope_count=1`, added only `47` metrics, and reintroduced `0` identity/topology rows for 2026-06-12. The long-running source stack on `10.13.37.138:8080/8081/8082` was also refreshed with the same hotfix code before cleanup, then backed up to `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-pre-noise-cleanup-20260612T165534Z.sqlite3`; source cleanup deleted `2,422` same-day noisy rows (`1,850` identity, `572` topology), preserved `946` same-day state-change rows, left exact source counts at `347` tracked slots, `18,109` events, `1,379,449` metric samples, and a post-cleanup fast refresh reintroduced `0` identity/topology rows. | Pass |  |
| Restored Linux QA perf harnesses | yes | after corrected full-data restore and `:18081/healthz` `collection_running=false`, reran serial perf harnesses: `.venv/bin/python scripts/run_perf_harness.py --base-url http://10.13.37.138:18080 --iterations 3 --format markdown --label release-candidate-linux-qa-fullsource` wrote `data/perf/latest.md` (`inventory_cached` avg `8.4 ms`, `inventory_force` avg `52099.9 ms`, `storage_views_cached` avg `26.5 ms`, `snapshot_export_estimate` avg `667.6 ms`); `.venv/bin/python scripts/run_history_perf_harness.py --base-url http://10.13.37.138:18081 --iterations 3 --format markdown --label release-candidate-history-linux-qa-fullsource` wrote `data/history-perf/latest.md` (`overview_estimated` avg `3.3 ms`, DB `997.6 MiB`, `347` tracked slots, `20,409` estimated slot events, `1,372,353` metric samples, `collection_running=false`) | Pass |  |
| Snapshot/export/offline artifact gate | yes | local mechanics smoke remains as previously recorded; corrected full-data Linux QA stack repeated snapshot estimate/download/offline smoke against `http://10.13.37.138:18080`: forced ZIP artifact `artifacts/private-v0.21.0/linux-qa-fullsource-snapshot-export/linux-qa-fullsource-snapshot-export-20260612T114504Z.zip` was `1,146,288` bytes with SHA-256 `f62eac9d8b6fb3010b76c63b8718fc025f34d0595c49177a771412263516fefd`; estimate reported HTML `9,844,106` bytes and ZIP `1,146,288` bytes; extracted offline HTML opened in Playwright with `11` system options, `60` tiles, `2` storage-view options, `2` live-enclosure options, a `Frozen Offline Artifact` banner, no horizontal overflow, and no browser error/warning console messages | Pass |  |
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
