# Release Wrap - v0.20.2

Date: `2026-05-21`

## Scope

`v0.20.2` is a corrective public release for the release process itself.

The release keeps `v0.20.1` Storage Fabric runtime behavior intact and ships
the global release-gate hardening that prevents future tags from skipping or
silently narrowing `docs/RELEASE_CHECKLIST.md`.

Included changes:

- app/package metadata bump to `0.20.2`
- `docs/RELEASE_CHECKLIST.md` as mandatory source of truth for every tag
- release-wrap evidence-table requirements for every future release
- `scripts/validate_release_wrap.py` plus regression coverage
- `docs/RELEASE_WRAP_0.20.1.md` post-publish checklist audit
- `docs/RELEASE_NOTES_0.20.2.md`

No new Storage Fabric platform enrichment, UI workflow, or write-capable action
is introduced by this patch.

## Checklist Evidence

Validated against `docs/RELEASE_CHECKLIST.md`.

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | hotfix branch `codex/v0.20.2-release-process-correction-2026-05-21` from `main` tag `v0.20.1` commit `011bd1d`; corrective SemVer patch version `0.20.2`; no unrelated scratch files staged | Pass |  |
| Python unit and syntax gates | yes | `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v` passed `466` tests after the pre-tag validator update; `python -m compileall app admin_service scripts tests` passed; `git diff --check` passed after LF-normalizing the regenerated demo artifact | Pass |  |
| JavaScript syntax gates | yes | `node --check` passed for `app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` | Pass |  |
| Docker build and health gates | yes | local `docker compose -f docker-compose.dev.yml --profile history --profile admin up -d --build --force-recreate`; UI, history, and admin `/livez` reported `0.20.2`; UI/admin `/healthz` ok and history settled after startup collection | Pass |  |
| Optional-sidecar runtime matrix | yes | UI-only livez/healthz plus main smoke passed; UI+history livez/healthz/status plus 2 history UI smokes passed after startup settle; UI+admin health plus admin operations and ESXi setup smokes passed with one startup/preload timeout followed by immediate rerun pass; full stack health plus browser suite passed with the startup-skipped history case rerun green | Pass |  |
| Full Playwright/browser gates | yes | local full stack `npx playwright test` passed `26` with `1` startup-readiness skip and the skipped history dashboard path rerun green; restored Linux QA full suite passed `26` with `1` perf-config skip, then `auto-refresh does not immediately fire after a system switch` passed after enabling `PERF_TIMING_ENABLED=true` in the disposable QA stack; `artifacts/release-0.20.2-linux-qa-playwright.json` records the restored run | Pass |  |
| Feature-specific live API/UI gates | yes | restored/local Storage Fabric API checks: CORE `archive-core` controllers `2`, paths `3`, traces `63`, links `467`; SCALE `offsite-scale` fabric `linux_ses`, paths `1`, traces `25`, links `99`; Quantastor `qsosn-ha` kind `storage_quantastor`, traces `25`, links `69`; ESXi `esxi-ft-node-2` kind `storage_esxi`, controllers `2`, paths `6`, traces `12`, links `28`; dedicated `/sas-fabric` Disk Path and Impact Map browser smoke covered CORE, SCALE, Quantastor, and ESXi with no console/page errors and screenshots under `artifacts/release-0.20.2-storage-fabric-*.png` | Pass |  |
| Local release perf harnesses | yes | local labels `release-candidate-0.20.2-local` and `release-candidate-0.20.2-history-local`; main averages included `health_cached 12.8 ms`, `storage_views_cached 47.9 ms`, `inventory_force 28554.1 ms`; history averages included `sidecar_healthz 16.4 ms`, `dashboard_html 107.3 ms`; CSV/JSONL trails written under `data/perf/` and `data/history-perf/` | Pass |  |
| Linux QA restore gate | yes | exported restore-grade backup through local admin API to `artifacts/release-0.20.2-jbod-system-backup-20260521T041437Z.tar.zst`; restored via disposable Linux stack on `codex-dev-test-target` under `/docker-local/truenas-jbod-ui-qa/v0.20.2-20260521T0418` using ports `18080/18081/18082`; restored counts: systems `11`, storage views `12`, custom profile file entries `2`, SAS alias groups `3`, slot-detail cache entries `3`, history metric samples `877622`, slot events `251`, current slot states `287`; UI/history/admin livez `0.20.2` and health ok after isolated secret material copy | Pass |  |
| Restored Linux QA perf harnesses | yes | restored labels `release-candidate-0.20.2-linux-qa-restore` and `release-candidate-0.20.2-history-linux-qa-restore`; main averages included `health_cached 3.7 ms`, `storage_views_cached 26.9 ms`, `inventory_force 24346.1 ms`; history averages included `dashboard_html 7.8 ms`, `sidecar_healthz 15.4 ms`, `overview_estimated 19.9 ms`; restored worktree reported clean in both harnesses | Pass |  |
| Snapshot/export/offline artifact gate | yes | restored Linux QA export estimate used Auto packaging and selected HTML at `6.2 MiB`; forced ZIP download from the same snapshot inputs produced `artifacts/release-0.20.2-linux-qa-offline-snapshot.zip` at `1064709` bytes and extracted `release-0.20.2-linux-qa-offline-snapshot.html`; Playwright opened the exported HTML from disk, found frozen offline snapshot copy, and saw no console/page errors | Pass |  |
| Docs/wiki/public-demo gate | yes | `docs/RELEASE_CHECKLIST.md`, `scripts/validate_release_wrap.py`, validator tests, `docs/RELEASE_NOTES_0.20.2.md`, this wrap, changelog/version metadata, and `public-demo/index.html` refreshed; public demo freshness check passed, publishability checker passed, and `PUBLIC_DEMO_ARTIFACT=public-demo/index.html npx playwright test qa/public-demo.spec.js` passed; `wiki/` source unchanged, so no external wiki sync is required before tag | Pass |  |
| GHCR publish verification | yes | post-publish gate pending the public tag and GitHub release; pre-tag validator allows only this inherently post-publish blocker | Blocked |  |
| Deployment refresh/sniff tests | yes | post-publish gate pending the `v0.20.2` GHCR image; local, Linux, and production refresh/sniff tests will run after digest verification | Blocked |  |
| Post-release reopen | yes | post-publish gate pending tag, GHCR verification, deployment sniff tests, and final wrap update | Blocked |  |

## Publish Result

Pre-tag gate is ready once
`.\.venv\Scripts\python.exe scripts\validate_release_wrap.py 0.20.2 --phase pre-tag`
passes.

Final publish result is pending the public tag, GitHub release, GHCR digest
verification, deployment refresh/sniff tests, and development-branch reopen.

## Notes

- `v0.20.1` is intentionally left intact. Deleting, overwriting, or retagging a
  public release would make the operator/audit trail less trustworthy.
- `0.20.1.1` was not used as the app/package version because the repo metadata
  uses SemVer-compatible versions; `0.20.2` is the SemVer-safe corrective patch.
