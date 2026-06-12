# Release Wrap - v0.21.1

Date: `2026-06-12`

## Scope

`v0.21.1` is a narrow SemVer patch on top of the published `v0.21.0` release. It fixes the post-publish history-noise class discovered while refreshing the long-running `.138` source deployment from GHCR: transient present/absent UNVR Pro slot flaps produced paired `slot_identity_changed` and `slot_topology_changed` rows even though disk identity and topology were restored on the next pass.

The patch intentionally does not change operator-facing layout, Storage Fabric routing, backup/import behavior, Quantastor visibility, public demo data, or release screenshots.

Release commit: `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c`

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | release branch `codex/v0.21.0-release-final-20260611` and `main` both pointed at `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c` when `v0.21.1` was tagged; only code behavior change is suppressing non-state history event groups when `previous.present != current.present` | Pass |  |
| Python unit and syntax gates | yes | `.venv/bin/python -m unittest tests.test_history_service.HistoryDomainTests -q` passed `6`; `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -q` passed `513` tests with `4` skipped; `.venv/bin/python -m compileall -q history_service tests app` passed | Pass |  |
| JavaScript syntax gates | yes | `npm ci --ignore-scripts` passed with `0` vulnerabilities; `node --check app/static/app.js`, `app/static/sas_fabric_view.js`, `admin_service/static/admin.js`, and `qa/public-demo.spec.js` passed; `git diff --check` passed | Pass |  |
| Docker build and health gates | yes | local image `truenas-jbod-ui:v0.21.1-hotfix-local` built from the hotfix source; temporary UI container returned `/livez status=ok version=0.21.1` and `/healthz status=ok` | Pass |  |
| Optional-sidecar runtime matrix | yes | temporary local `0.21.1` stack booted UI/history/admin sidecars on `127.0.0.1:19180/19181/19182`; UI/history/admin `/livez` returned `status=ok version=0.21.1`; history/admin `/healthz` returned `status=ok`; `/api/history/status` returned JSON | Pass |  |
| Full Playwright/browser gates | yes | no browser/UI code changed in `v0.21.1`; `v0.21.0` full restored Linux QA Playwright gate remains the UI/layout evidence, and the hotfix local image served `/` HTML successfully (`159640` bytes) | Pass |  |
| Feature-specific live API/UI gates | yes | `.138` source stack rebuilt from hotfix source and served `http://10.13.37.138:8080/livez` as `status=ok version=0.21.1`; history sidecar cleaned and refreshed with `0` same-day `slot_identity_changed`/`slot_topology_changed` rows after the presence-flap regression fix | Pass |  |
| Local release perf harnesses | yes | inherited from `v0.21.0` release wrap because this patch changes only event grouping for present/absent transitions and does not affect inventory/API rendering paths; local Docker health/sniff was rerun for `0.21.1` | Pass |  |
| Linux QA restore gate | yes | inherited `v0.21.0` corrected full-data Linux QA restore/provenance gate; `v0.21.1` additionally rebuilt the long-running `.138` source stack from hotfix source and preserved `347` tracked slots while cleaning only the 8 post-`v0.21.0` identity/topology rows | Pass |  |
| Restored Linux QA perf harnesses | yes | inherited `v0.21.0` restored Linux QA perf evidence; `v0.21.1` is a narrow history event-filter patch with no perf-sensitive path change | Pass |  |
| Snapshot/export/offline artifact gate | yes | inherited `v0.21.0` snapshot/export/offline artifact evidence because `v0.21.1` does not change export, render, or offline HTML code | Pass |  |
| Docs/wiki/public-demo gate | yes | `CHANGELOG.md`, `docs/RELEASE_NOTES_0.21.1.md`, and this release wrap added/updated; public demo artifact unchanged and public-demo workflow is not expected to run for release-note-only/docs changes outside `public-demo/**` | Pass |  |
| GHCR publish verification | yes | GitHub Release `v0.21.1` published at `2026-06-12T18:10:16Z`; release-triggered GHCR workflow `27434127617` succeeded; `v0.21.1`, `0.21.1`, and `latest` all pulled as digest `sha256:28e38a92dd77b9526cf2367bf151b44fafa85a82e425434fdbdc95c56a6ac6d1` with OCI revision `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c`, version `0.21.1` | Pass |  |
| Deployment refresh/sniff tests | yes | local published-digest stack sniff passed for UI/history/admin `/livez` and `/healthz`; `.138` source `8080/8081/8082` and full-data QA `18080/18081/18082` were both recreated from `ghcr.io/gcs8/truenas-jbod-ui@sha256:28e38a92dd77b9526cf2367bf151b44fafa85a82e425434fdbdc95c56a6ac6d1`; both served UI `/livez status=ok version=0.21.1`, history/admin health `status=ok`, and reintroduced `0` same-day identity/topology rows | Pass |  |
| Post-release reopen | yes | after release/deployment verification, `main` development metadata was reopened as `0.21.2-dev` in `app/__init__.py`, `package.json`, and `package-lock.json`; `v0.21.1` tag remains on the immutable release commit | Pass |  |

## Publish Result

- Release commit: `cfb92f2576f7c0d0d7fdd3b3ff58918897c0fe7c`
- Tag: `v0.21.1`
- GitHub release: `https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.21.1`
- GHCR workflow: `https://github.com/gcs8/truenas-jbod-ui/actions/runs/27434127617`
- GHCR digest: `sha256:28e38a92dd77b9526cf2367bf151b44fafa85a82e425434fdbdc95c56a6ac6d1`
- Verified tags: `ghcr.io/gcs8/truenas-jbod-ui:v0.21.1`, `ghcr.io/gcs8/truenas-jbod-ui:0.21.1`, and `ghcr.io/gcs8/truenas-jbod-ui:latest` all converged to the digest above
- Published source deployment: `10.13.37.138:8080/8081/8082`, all containers pinned to the digest above, `347` tracked slots, `18,243` events, `1,381,523` metric samples, `0` same-day identity/topology rows after refresh
- Published QA deployment: `10.13.37.138:18080/18081/18082`, all containers pinned to the digest above, `347` tracked slots, `17,841` events, `1,372,400` metric samples, `0` same-day identity/topology rows after refresh
- Issue closed: `https://github.com/gcs8/truenas-jbod-ui/issues/6` closed as completed at `2026-06-12T18:21:42Z`
- Supersedes: `v0.21.0` for deployment; `v0.21.0` remains published for audit history and should not be used as the final candidate
- Source cleanup backup: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-pre-presence-flap-cleanup-20260612T175900Z.sqlite3`
- Source cleanup manifest: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-presence-flap-cleanup-20260612T175900Z.json`
- Source hotfix rollback dirs:
  - `/srv/truenas-jbod-ui/migrations/rollback-pre-presence-flap-history-20260612T175900Z`
  - `/srv/truenas-jbod-ui/migrations/rollback-pre-presence-flap-history-20260612T175900Z-corrected`

## Notes

The `v0.21.0` public release briefly existed before this presence-flap class was found during post-publish deployment refresh. Following the repository release checklist, this remediation ships as a SemVer patch rather than rewriting the already-published `v0.21.0` artifact.
