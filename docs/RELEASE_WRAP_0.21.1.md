# Release Wrap - v0.21.1

Date: `2026-06-12`

## Scope

`v0.21.1` is a narrow SemVer patch on top of the published `v0.21.0` release. It fixes the post-publish history-noise class discovered while refreshing the long-running `.138` source deployment from GHCR: transient present/absent UNVR Pro slot flaps produced paired `slot_identity_changed` and `slot_topology_changed` rows even though disk identity and topology were restored on the next pass.

The patch intentionally does not change operator-facing layout, Storage Fabric routing, backup/import behavior, Quantastor visibility, public demo data, or release screenshots.

Release commit: `tag target v0.21.1 (resolved after tag)`

Validated against `docs/RELEASE_CHECKLIST.md`.

## Checklist Evidence

| Gate | Required | Evidence | Result | N/A Reason |
| --- | --- | --- | --- | --- |
| Scope and branch | yes | hotfix branch `codex/v0.21.0-release-final-20260611` after `v0.21.0` publication; version bumped to `0.21.1`; only code behavior change is suppressing non-state history event groups when `previous.present != current.present`; release commit `tag target v0.21.1 (resolved after tag)` | Pass |  |
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
| GHCR publish verification | yes | post-publish gate: blocked until `v0.21.1` GitHub release triggers GHCR and `v0.21.1`, `0.21.1`, and `latest` digests are recorded | Blocked |  |
| Deployment refresh/sniff tests | yes | post-publish gate: blocked until the published `v0.21.1` GHCR image is pulled/sniffed locally and deployed/sniffed on the `.138` long-running source stack | Blocked |  |
| Post-release reopen | yes | post-publish gate: blocked until next development version/branch is reopened after `v0.21.1` publish/deployment verification | Blocked |  |

## Publish Result

- Release commit: `tag target v0.21.1 (resolved after tag)`
- Tag: `v0.21.1` when cut
- GitHub release: `TBD`
- GHCR digest: `TBD`
- Supersedes: `v0.21.0` for deployment; `v0.21.0` remains published for audit history and should not be used as the final candidate
- Source cleanup backup: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-pre-presence-flap-cleanup-20260612T175900Z.sqlite3`
- Source cleanup manifest: `/srv/truenas-jbod-ui/history/manual-cleanup-backups/source-history-presence-flap-cleanup-20260612T175900Z.json`
- Source hotfix rollback dirs:
  - `/srv/truenas-jbod-ui/migrations/rollback-pre-presence-flap-history-20260612T175900Z`
  - `/srv/truenas-jbod-ui/migrations/rollback-pre-presence-flap-history-20260612T175900Z-corrected`

## Notes

The `v0.21.0` public release briefly existed before this presence-flap class was found during post-publish deployment refresh. Following the repository release checklist, this remediation ships as a SemVer patch rather than rewriting the already-published `v0.21.0` artifact.
